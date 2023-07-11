import argparse
import yaml
from pathlib import Path

import numpy as np
from functools import partial
import jax
import jax.numpy as jnp
import haiku as hk
import optax
from tempfile import mkstemp
from lib.lion import lion
from collections import defaultdict
from PIL import Image
import matplotlib as mpl
from einops import rearrange

import wandb
from tqdm import tqdm

from munch import munchify
from lib.data_loading import get_datasets, get_unlabelled
from lib import utils, logging, losses
from lib.config_mod import config
from lib.metrics import compute_premetrics
from lib.utils import AdversarialState, prep, distort, changed_state, save_state
from lib.models.discriminator import Discriminator

jax.config.update("jax_numpy_rank_promotion", "raise")


def get_optimizer():
  conf = config.optimizer
  schedule = getattr(optax, conf.schedule)(**conf.schedule_args)
  opt_class = getattr(optax, conf.type)
  return opt_class(schedule, **conf.args)


def D_optimizer():
  conf = config.d_optimizer
  schedule = getattr(optax, conf.schedule)(**conf.schedule_args)
  opt_class = getattr(optax, conf.type)
  return opt_class(schedule, **conf.args)


def get_loss_fn(mode):
  name = config.loss_functions[f'{mode}']
  return getattr(losses, name)


@jax.jit
def train_step(data, unlabelled, state, key):
  _, optimizer = get_optimizer()

  key_1a, key_1b, key_2, key_3, key_4 = jax.random.split(key, 5)

  batch = distort(prep(data, key_1a), key_1b)
  img, mask = batch['s2'], batch['mask']
  img_2 = distort(prep(unlabelled, key_2), key_3)['img']

  def get_loss(params):
    terms = {}
    pred_true = model(params, img)
    pred_fake = model(params, img_2)
    judgement = D(state.D_params, pred_fake)

    terms['loss_super'] = get_loss_fn('train')(mask, pred_true)
    terms['loss_semi'] = jnp.mean(-jax.nn.log_sigmoid(pred_fake))

    terms['loss'] = terms['loss_super'] + \
        config.train.unlabelled_weight * terms['loss_semi']
    terms['super_premetrics'] = compute_premetrics(mask, pred_true)

    return terms['loss'], (terms, pred_fake)

  gradients, (terms, pred_fake) = jax.grad(get_loss, has_aux=True)(state.params)
  updates, new_opt = optimizer(gradients, state.opt, state.params)
  new_params = optax.apply_updates(state.params, updates)

  def discriminator_loss(d_params):
    out_true = D(d_params, jnp.where(mask >= 2, 0, mask).astype(jnp.float32))
    out_fake = D(d_params, pred_fake)
    loss = jnp.mean(-jax.nn.log_sigmoid(-out_fake) - jax.nn.log_sigmoid(out_true))
    return loss

  terms['loss_discriminator'], D_gradients = jax.value_and_grad(discriminator_loss)(state.D_params)
  updates, D_opt = optimizer(D_gradients, state.D_opt, state.D_params)
  D_params = optax.apply_updates(state.D_params, updates)

  # EMA steps
  return terms, changed_state(state,
      params=new_params,
      opt=new_opt,
      D_params=D_params,
      D_opt=D_opt,
  )


@jax.jit
def test_step(batch, state):
    batch = prep(batch)
    img = batch['s2']
    mask = batch['mask']

    pred = model(state.params, img)
    loss = get_loss_fn('train')(mask, pred)

    terms = {
        'loss': loss,
        'val_premetrics': compute_premetrics(mask, pred),
    }

    return terms, jax.nn.sigmoid(pred)


if __name__ == '__main__':
  parser = argparse.ArgumentParser(prog="Permafrost SSL Training script")
  parser.add_argument('config', type=Path)
  parser.add_argument('-s', '--seed', type=int, required=True)
  parser.add_argument('-n', '--name', type=str, required=True)
  parser.add_argument('-f', '--skip-git-check', action='store_true')
  parser.add_argument('-w', '--unlabelled_weight', type=float)
  args = parser.parse_args()

  train_key = jax.random.PRNGKey(args.seed)
  persistent_val_key = jax.random.PRNGKey(27)

  config.update(munchify(yaml.safe_load(args.config.open())))
  if args.unlabelled_weight is not None:
    config.train.unlabelled_weight = args.unlabelled_weight

  # initialize data loading
  train_key, subkey = jax.random.split(train_key)

  datasets = get_datasets(config['datasets'])
  val_data = {k: datasets[k] for k in datasets if k.startswith('val')}
  trn_data = datasets['train']

  unlabelled_data = get_unlabelled(config['train']['unlabelled_bs'])

  S, params = utils.get_model(np.ones([1, 64, 64, 12]))

  discriminator = hk.without_apply_rng(hk.transform(Discriminator()))
  D_params = jax.jit(discriminator.init)(jax.random.PRNGKey(31), np.ones([1, 64, 64, 1]))
  D = discriminator.apply

  # Initialize model and optimizer state
  opt_init, _ = get_optimizer()
  model = S.apply

  state = AdversarialState(params=params, opt=opt_init(params),
                           D_params=D_params, D_opt=opt_init(D_params))

  wandb.init(project=f'PixelDINO', config=config, name=args.name, group=config.train.group)

  run_dir = Path(f'runs/{wandb.run.id}/')
  assert not run_dir.exists(), f"Previous run exists at {run_dir}"

  run_dir.mkdir(parents=True)
  config.run_id = wandb.run.id
  with open(run_dir / 'config.yml', 'w') as f:
      f.write(yaml.dump(config, default_flow_style=False))

  trn_gen = jax.tree_map(iter, trn_data)
  unlabelled_gen = iter(unlabelled_data)
  trn_metrics = defaultdict(list)
  for step in tqdm(range(1, 1+config.train.steps), ncols=80):
    data = next(trn_gen)
    data = {'s2': data['s2'], 'mask': data['mask']}
    unlabelled = next(unlabelled_gen)
    unlabelled = {'img': unlabelled['img']}

    train_key, subkey = jax.random.split(train_key)
    terms, state = train_step(data, unlabelled, state, subkey)

    for k in terms:
        trn_metrics[k].append(terms[k])

    # """
    # Metrics logging and Validation
    # """
    if step % config.validation.frequency != 0:
      continue

    logging.log_metrics(trn_metrics, 'trn', step, do_print=False)
    trn_metrics = defaultdict(list)

    for tag, dataset in val_data.items():
      # Validate
      val_key = persistent_val_key
      val_metrics = defaultdict(list)
      val_outputs = defaultdict(list)
      for step_test, data in enumerate(dataset):
          val_key, subkey = jax.random.split(val_key, 2)
          data_inp = {'s2': data['s2'], 'mask': data['mask']}
          metrics, preds = test_step(data_inp, state)

          for m in metrics:
            val_metrics[m].append(metrics[m])

          for i in range(preds.shape[0]):
            key = data['source'][i].decode('utf8')
            val_outputs[key].append({
              'pred': preds[i],
              **jax.tree_map(lambda x: x[i], data),
            })

      logging.log_metrics(val_metrics, tag, step)

      if step % config.validation.image_frequency != 0:
        continue

      # Save Checkpoint
      save_state(state, run_dir / f'step_{step:07d}.pkl')
      save_state(state, run_dir / f'latest.pkl')

      for tile, data in val_outputs.items():
        name = Path(tile).stem
        y_max = max(d['box'][3] for d in data)
        x_max = max(d['box'][2] for d in data)

        weight = np.zeros([y_max, x_max, 1], dtype=np.float64)
        rgb    = np.zeros([y_max, x_max, 3], dtype=np.float64)
        mask   = np.zeros([y_max, x_max, 1], dtype=np.float64)
        pred   = np.zeros([y_max, x_max, 1], dtype=np.float64)

        window = np.concatenate([
          np.linspace(0, 1, 96),
          np.linspace(0, 1, 96)[::-1],
        ]).reshape(-1, 1)
        stencil = (window * window.T).reshape(192, 192, 1)

        for patch in data:
          x0, y0, x1, y1 = patch['box']
          patch_rgb  = patch['s2'][:, :, [3,2,1]]
          patch_rgb  = np.clip(patch_rgb, 0, 255)
          patch_mask = np.where(patch['mask'] == 127, 64, patch['mask'])
          patch_mask = np.clip(patch_mask, 0, 255)
          patch_pred = np.clip(255 * patch['pred'], 0, 255)

          patch_rgb = np.asarray(patch_rgb).astype(np.float64)
          patch_mask = np.asarray(patch_mask).astype(np.float64)
          patch_pred = np.asarray(patch_pred).astype(np.float64)

          weight[y0:y1, x0:x1] += stencil
          rgb[y0:y1, x0:x1]    += stencil * patch_rgb
          mask[y0:y1, x0:x1]   += stencil * patch_mask
          pred[y0:y1, x0:x1]   += stencil * patch_pred

        weight = np.where(weight == 0, 1, weight)
        rgb  = np.clip(rgb / weight, 0, 255).astype(np.uint8)
        mask = np.clip(mask / weight, 0, 255).astype(np.uint8)
        pred = np.clip(pred / weight, 0, 255).astype(np.uint8)

        stacked = np.concatenate([mask, pred, np.zeros_like(mask)], axis=-1)
        stacked = Image.fromarray(stacked)
        _, stacked_jpg = mkstemp('.jpg')
        stacked.save(stacked_jpg)

        @jax.jit
        def mark_edges(mask, threshold):
          mask = (mask > threshold).astype(np.float32)
          if mask.ndim > 2:
            mask = mask[..., 0]
          padded = jnp.pad(mask, 1, mode='edge')
          padded = rearrange(padded, 'H W -> 1 H W 1')
          min_pooled = -hk.max_pool(-padded, 3, 1, 'VALID')
          max_pooled = hk.max_pool(padded, 3, 1, 'VALID')
          is_edge = min_pooled != max_pooled
          is_edge = rearrange(is_edge, '1 H W 1 -> H W')
          return 255 * is_edge.astype(np.uint8)

        mask_img = mark_edges(mask, 0.5)
        pred_img = mark_edges(pred, 0.7 * 255)
        annot = np.stack([
          mask_img,
          pred_img,
          np.zeros_like(mask_img),
        ], axis=-1)
        contour_img = np.where(np.all(annot == 0, axis=-1, keepdims=True), rgb, annot)
        contour_img = Image.fromarray(contour_img)
        _, contour_jpg = mkstemp('.jpg')
        contour_img.save(contour_jpg)

        wandb.log({f'contour/{name}': wandb.Image(contour_jpg),
                   f'imgs/{name}': wandb.Image(stacked_jpg),
        }, step=step)
