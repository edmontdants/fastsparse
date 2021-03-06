# AUTOGENERATED! DO NOT EDIT! File to edit: 00_core.ipynb (unless otherwise specified).

__all__ = ['sparse_mask', 'sparse_mask_like', 'mask_from_tensor', 'sparsity_from_tensor', 'maybe_float',
           'sparse_params', 'apply_masks', 'is_sparseable_module', 'sparseable_modules', 'mask_from_tensor',
           'sparsity_from_tensor', 'init_kaiming_normal_sparse_', 'uniform_sparsity', 'first_layer_dense_uniform',
           'erdos_renyi_sparsity', 'sparsify_model', 'random_score', 'weight_magnitude', 'gradient_magnitude',
           'gradient_momentum', 'momentum_redistribution', 'top_k_mask', 'DynamicSparseTrainingCallback', 'SET_presets',
           'SNFS_presets', 'RigL_presets', 'flop_counter_hook', 'sparse_flop_counter_hook', 'count_flops',
           'FlopsCounter']

# Cell
import numpy as np
import torch
import torch.nn as nn

# Cell
from fastcore.all import *
from fastai.basics import *
from fastai.vision.all import *
from fastai.callback.all import *
from fastai.test_utils import *

# Cell
@torch.no_grad()
def sparse_mask(sizes, sparsity):
    '''Returns a boolean mask with uniformly distributed zeros. # zeros = `sparsity` * np.prod(`sizes`)'''
    n_total = np.prod(sizes)
    n_ones = round((1-sparsity) * n_total)
    shuffled_ones = torch.randperm(n_total)[:n_ones]
    mask = torch.zeros(n_total, dtype=torch.bool)
    mask[shuffled_ones] = True
    return mask.reshape(*sizes)

def sparse_mask_like(param, sparsity): return sparse_mask(param.shape, sparsity).to(param.device)
def mask_from_tensor(t): return t.ne(0)
def sparsity_from_tensor(t): return 1 - mask_from_tensor(t).sum() / t.numel()

# Cell
def maybe_float(num):
    try: return float(num)
    except: return num

def sparse_params(module):
    '''Returns list of all (param, mask, sparsity) tuples in a module.'''
    buffer_d = {name:b for name, b in module.named_buffers()}
    param_mask_sparsities = [(p, buffer_d[f'{name}_mask'], maybe_float(buffer_d.get(f'{name}_sparsity')))
                             for name, p in module.named_parameters()
                             if f'{name}_mask' in buffer_d]
    return list(set(param_mask_sparsities))

# Cell
@torch.no_grad()
def apply_masks(module, *args, inplace=True):
    for param, mask, sparsity in sparse_params(module):
        if inplace: param.data.mul_(mask)
        else:       param.data = param.data.mul(mask)

# Cell
_sparseable_module_types = (nn.Linear,
                            nn.Conv1d, nn.Conv2d, nn.Conv3d,
                            nn.ConvTranspose1d, nn.ConvTranspose2d, nn.ConvTranspose3d,
                            nn.MultiheadAttention,
                            nn.RNN, nn.RNNCell, nn.GRU, nn.GRUCell, nn.LSTM, nn.LSTMCell)

def is_sparseable_module(m, additional_types=[]):
    types = set(_sparseable_module_types) | set(additional_types)
    return isinstance(m, tuple(types))

# Cell

# TODO: flatten_model gets rid of nn.MultiheadAttention which has it's own parameter 'in_proj_weight'
#       which means sparsity_model doesn't sparsify this parameter
def sparseable_modules(model, additional_types=[]):
    filt = partial(is_sparseable_module, additional_types=additional_types)
    return L(flatten_model(model)).filter(filt)

# Cell
def mask_from_tensor(t): return t != 0
def sparsity_from_tensor(t): return 1 - mask_from_tensor(t).sum() / t.numel()

# Cell
@torch.no_grad()
def init_kaiming_normal_sparse_(t, a=0, mode='fan_in', sparse_mode='fan_in_out', nonlinearity='leaky_relu'):
    '''A modified kaiming normal initialization which adjusts for sparsity in weights.'''
    # calculate sparse adjustment to standard deviation
    #  dense kaiming init = mode / sqrt(dense_fan), e.g. for relu = 2 / sqrt(dense_fan)
    #  sparse kaiming init = mode / sqrt(sparse_fan), note: sparse fan is unique to each input/output
    #                      = (dense kaiming init) * sqrt(dense_fan / sparse_fan)
    mask = mask_from_tensor(t)
    mode = mode if mask.sum() == t.numel() else sparse_mode
    mode_ix = ['fan_in', 'fan_out', 'fan_in_out'].index(mode)
    dim = [1,0,1][mode_ix]

    dense_fan = t.shape[dim] * t[0][0].numel()

    sparse_fan_in = mask.sum(1, keepdim=True)
    sparse_fan_out = mask.sum(0, keepdim=True)
    # variance of 'fan_in_out' is harmonic mean of 'fan_in' and 'fan_out'
    sparse_fan_in_out = (sparse_fan_in + sparse_fan_out) / 2

    sparse_fan = [sparse_fan_in, sparse_fan_out, sparse_fan_in_out][mode_ix]
    sparse_fan[sparse_fan==0] = 1 # avoid div by 0, can set to anything since these are masked

    std_adj = torch.sqrt(dense_fan / sparse_fan)

    # initialize as dense, then apply mask and apply sparse adjustment
    mode = 'fan_in' if mode == 'fan_in_out' else mode
    nn.init.kaiming_normal_(t, a=a, mode=mode, nonlinearity=nonlinearity)
    return t.mul_(mask).mul_(std_adj)

# Cell
def uniform_sparsity(params, model_sparsity):
    return [model_sparsity] * len(params)

# Cell
def first_layer_dense_uniform(params, model_sparsity):
    sparsities = [0.] + [model_sparsity] * (len(params) - 1)
    return sparsities

# Cell
# modified from https://github.com/google-research/rigl/blob/master/rigl/sparse_utils.py.
def erdos_renyi_sparsity(params, model_sparsity, include_kernel=True, erk_power_scale=1.0):
    """
    Returns a list of sparsities in the same order as params. Sparsities satisfy
    the Erdos-Renyi(Kernel) distribution, where the model has a total parameter count
    as one with uniform sparsities, that is, satisfying the following equation:
    $ eps * (p_1 * N_1 + p_2 * N_2) = (1 - model_sparsity) * (N_1 + N_2) $, for some float `eps`.

    Args:
    params: list of all sparseable parameters
    model_sparsity: target overall sparsity between 0 and 1
    include_kernel: if True, kernel dimensions are included in the scaling (e.g. for ConvNd layers)
    erk_power_scale: scale < 1 softens the erdos_renyi distribution (i.e. closer to uniform)

    Returns a list of sparsities where values correspond to individual param sparsities.
    """
    # Enforce custom sparsities, then find correct scaling factor, `eps` for remaining params
    dense_layers = set()
    is_eps_valid = False
    while not is_eps_valid:
        # Start with all layers and try to find right eps. If any sparsity exceeds 1,
        # make that layer dense and repeat with the non-dense layers.
        #
        # E.g. where N_3, and N_4 are found to be dense:
        # eps * (p_1 * N_1 + p_2 * N_2) + (N_3 + N_4) =
        #    (1 - model_sparsity) * (N_1 + N_2 + N_3 + N_4)
        # eps * (p_1 * N_1 + p_2 * N_2) =
        #    (1 - model_sparsity) * (N_1 + N_2) - model_sparsity * (N_3 + N_4) <--- == rhs
        # eps = rhs / (\sum_i p_i * N_i) <--- == divisor
        # eps = rhs / divisor

        divisor = 0
        rhs = 0
        raw_sparsity = {}
        for p in params:
            n_zeros = int(np.floor(model_sparsity * p.numel()))
            if p in dense_layers:
                rhs -= n_zeros
            else:
                n_ones = p.numel() - n_zeros
                rhs += n_ones
                if include_kernel:
                    raw_sparsity[p] = (np.sum(p.shape) / np.prod(p.shape))**erk_power_scale
                else:
                    raw_sparsity[p] = (np.sum(p.shape[:2]) / np.prod(p.shape[:2]))
                divisor += raw_sparsity[p] * p.numel()

        eps = rhs / divisor

        # If eps * raw_sparsity[p] > 1, we add the param to the set of dense_layers
        max_sparsity = np.max(list(raw_sparsity.values()))
        if eps * max_sparsity > 1:
            for p, p_raw_sparsity in raw_sparsity.items():
                if p_raw_sparsity == max_sparsity:
                    dense_layers.add(p)
        else:
            is_eps_valid = True

    # With the valid eps, we can set sparsities of the remaining layers
    sparsities = [0. if p in dense_layers else (1. - eps * raw_sparsity[p]) for p in params]
    return sparsities

# Cell
@torch.no_grad()
def sparsify_model(model, model_sparsity, sparse_f=uniform_sparsity,
                   sparse_init_mode=None, enforce_mask=True):
    '''
    Adds a sparse mask for each sparseable-module weight in model and applies mask to weights.

    `sparse_f`: per RigL paper, `uniform_sparsity` has fewer FLOPs, `erdos_renyi_sparsity`
    results in better model.

    `sparse_init_mode`: initialization mode of sparse modules, or no initialization if None.
    Possible values: [None, 'fan_in', 'fan_out', 'fan_in_out']

    If `enforce_mask` is True, a forward_pre_hook will be registered to each module
    to apply the weight mask before every forward pass of the module.

    Returns a fastai Hooks object. You can remove the hooks after training by calling hooks.remove().
    '''
    if isinstance(model, Learner): model = model.model
    modules = sparseable_modules(model)
    module_name_param = L([(m, p_name, p) for m in modules for p_name, p in m.named_parameters()
                         if 'weight' in p_name])
    params = module_name_param.itemgot(2)
    sparsities = sparse_f(params, model_sparsity)

    hooks = Hooks([], noop)
    for (m, p_name, p), s in zip(module_name_param, sparsities):
        if s > 0:
            mask = sparse_mask_like(m.weight, s)
            m.register_buffer('weight_mask', mask)
            m.register_buffer('weight_sparsity', tensor(s))
            apply_masks(m)
            if sparse_init_mode is not None:
                init_f = partial(init_kaiming_normal_sparse_, sparse_mode=sparse_init_mode)
                init_default(m, func=init_f)
                apply_masks(m)
            if enforce_mask:
                h = m.register_forward_pre_hook(apply_masks)
                hooks.hooks.append(h)

    return hooks

# Cell
def random_score(p, **kwargs): return torch.rand_like(p)

# Cell
def weight_magnitude(p, **kwargs): return p.data.abs()

# Cell
def gradient_magnitude(p, **kwargs): return p.grad.abs()

# Cell
def gradient_momentum(p, opt, **kwargs):
    '''Calculates the momentum of the gradient for a parameter `p` from the `opt` state.'''
    state = opt.state[p]
    grad_avg = state['grad_avg'] if 'grad_avg' in state else None
    sqr_avg = state['sqr_avg'] if 'sqr_avg' in state else None
    if grad_avg is None:
        raise Exception(f"Error: 'grad_avg' key not found in optimizer state. Tip: set the `mom` hyperparamter in the learner.")
    if sqr_avg is None:
        grad_mom = grad_avg
    else:
        try: eps = opt.state_dict()['hypers'][0]['eps']
        except: eps = 1e-6
        grad_mom =  grad_avg / (torch.sqrt(sqr_avg + eps))
    return grad_mom

# Cell
def momentum_redistribution(dst_cb):
    '''
    Modifies each sparseable parameter's target sparsity proportional to its mean absolute momentum.

    Based on redistribution method in Sparse Networks From Scratch by Dettmers et al.
    (https://arxiv.org/abs/1907.04840). Instead of evenly distributing leftover weights, as in the
    official implementation, this method finds exact distribution amounts by making parameters dense
    one at a time until valid sparsities are found.
    '''
    param_d = {p: (mask, s, m) for m in dst_cb.modules for p,mask,s in sparse_params(m)}

    # calculate mean absolute momentum per layer and total # of params to distribute
    p2mom, p2drop, p2maxgrow = {}, {}, {}
    for p, (mask, s, m) in param_d.items():
            mom = gradient_momentum(p, dst_cb.learn.opt)
            mean_nonzero_mom = (mom * mask).abs().sum() / mask.sum()
            p2mom[p] = mean_nonzero_mom

            n_nonzeros = mask.sum()
            n_zeros = mask.numel() - n_nonzeros
            n_drop = int(n_nonzeros * dst_cb.drop_grow_pct)

            p2drop[p] = n_drop
            p2maxgrow[p] = n_zeros + n_drop

    # normalize momentum contributions to determine each parameters's growth factor
    total_mom = sum(p2mom.values())
    p2growth_factor = {p: float(mom / total_mom) for p, mom in p2mom.items()}

    total_n_drop = sum(p2drop.values())
    if total_n_drop == 0:
        return

    # Distribute weights proportional to parameter's momentum, without changing overall sparsity
    #   total_n_drop     = total_n_grow
    #   sum_p: n_drop[p] = sum_p: n_grow[p]
    #   sum_p: n_drop[p] = sum_dense_p: max_grow[p]
    #                    + eps * sum_sparse_p: growth_factor[p] * n_drop[p]
    # Goal is to find eps satisfying ^ this ^ equation where no layer's density > 1:
    #   eps = ( sum(n_drop[p]) - sum_dense_p(max_grow[p]) ) / sum_sparse_p(growth_factor[p] * n_drop[p])
    #   eps = (total_n_drop - total_dense_grow) / proportional_sparse_grow
    # Loop until no target density > 1, adding largest layer to dense set if not satisfied

#     print('dropping:', total_n_drop, 'individ:', p2drop.values())
    p2grow = {}
    dense_params = set()
    done = False
    while done == False:
        for p, (mask, s, m) in param_d.items():
            if p in dense_params:
                p2grow[p] = p2maxgrow[p] # = total_dense_grow[p]
            else:
                p2grow[p] = p2growth_factor[p] * p2drop[p] # = proportional_sparse_grow[p]

        # find eps
        total_dense_grow = sum(p2grow[p] for p in param_d.keys() if p in dense_params)
        proportional_sparse_grow = sum(p2grow[p] for p in param_d.keys() if p not in dense_params)
#         print('dense:', [p.numel() for p in dense_params])
        eps = (total_n_drop - total_dense_grow) / proportional_sparse_grow

        # find new sparsities
        p2sparsity = {}
        for p, (mask, s, m) in param_d.items():
            if p in dense_params:
                p2sparsity[p] = 0.
            else:
                n_drop = p2drop[p]
                n_grow = eps * p2grow[p]
                target_nonzeros = mask.sum() - n_drop + n_grow
                p2sparsity[p] = 1 - target_nonzeros / mask.numel()

        # if any sparse params have sparsity < 0 (i.e. denser than possible), move the lowest sparsity
        # param to the set of dense params, otherwise end loop
        min_sparsity = min([s for s in p2sparsity.values()])
        if min_sparsity < 0:
            for p, s in p2sparsity.items():
                if s == min_sparsity:
                    dense_params.add(p)
        else:
            done = True

    # set each parameter's sparsity buffer to new target sparsity
    for p, (mask, s, m) in param_d.items():
        pname = {param:pname for pname, param in m.named_parameters()}[p]
        sparsity_buffer = getattr(m, pname+'_sparsity')
        sparsity_buffer.data = torch.tensor(float(p2sparsity[p]))

# Cell
def top_k_mask(t, n_keep):
    '''Returns a mask with `n_keep` ones cooresponding to the largest values in `t`'''
    n_drop = t.numel() - n_keep
    _, sorted_ixs = torch.topk(t.flatten(), k=t.numel())
    mask = torch.cat([torch.ones(n_keep, dtype=torch.bool, device=t.device),
                      torch.zeros(n_drop, dtype=torch.bool, device=t.device)])
    mask = mask.scatter(0, sorted_ixs, mask)
    return mask.view(*t.shape)

# Cell
class DynamicSparseTrainingCallback(Callback):
    '''Dynamically updates the network connectivity during training.'''
    def __init__(self, sparse_modules=None,
                 batches_per_update=None, initial_drop_grow_pct=0.3, stop_pct=0.75,
                 keep_score_f=weight_magnitude, grow_score_f=gradient_magnitude, redistribute_f=None):
        store_attr('initial_drop_grow_pct,stop_pct,keep_score_f,grow_score_f,redistribute_f,batches_per_update')
        self.modules = sparse_modules

    def before_fit(self):
        self.modules = ifnone(self.modules, sparseable_modules(self.learn.model))
        self.batches_per_update = ifnone(self.batches_per_update, len(self.dls.train))
        self.drop_grow_pct_sched = combine_scheds(
            [self.stop_pct, 1-self.stop_pct],
            [SchedCos(self.initial_drop_grow_pct, 0.), SchedNo(0.,0.)]
        )
        self.n_param_count = sum([int(mask.numel()) for m in self.modules for _,mask,_ in sparse_params(m)])
        self.n_nonzeros = sum([int(mask.sum()) for m in self.modules for _,mask,_ in sparse_params(m)])
        self.model_sparsity = 1 - self.n_nonzeros / self.n_param_count

    def after_backward(self):
        self.step()
#         self.learn.opt.step()
        if self.is_update_step:
            if self.redistribute_f:
                self.redistribute_f(self)
            for m in self.modules:
                self.rewire_module(m)
            raise CancelBatchException()

    def step(self):
        if not self.training:
            self.is_update_step = False
        else:
            step = self.epoch * self.n_iter + self.iter
            n_steps = self.n_epoch * self.n_iter
            pct_train = step / n_steps
            is_last_step = step + 1 == n_steps
            self.is_update_step = (step > 0
                                   and step % self.batches_per_update == 0
                                   and self.drop_grow_pct > 0
                                   and not is_last_step)
            self.drop_grow_pct = self.drop_grow_pct_sched(pct_train)

    @torch.no_grad()
    def rewire_module(self, m):
        for param, mask, target_sparsity in sparse_params(m):

            current_sparsity = 1 - float(mask.sum() / mask.numel())
            n_grow = int(mask.sum() * self.drop_grow_pct)
            n_keep = mask.sum() - n_grow

#             modify n_grow if actual sparsity differs from target sparsity
            current_nonzeros = int(mask.sum())
            target_nonzeros = round(mask.numel() * (1 - target_sparsity))

            n_grow = max(0, n_grow + target_nonzeros - current_nonzeros)

            # determine which weights to keep
            if current_sparsity > 0 and target_sparsity > 0:
                keep_score = self.keep_score_f(param, opt=self.learn.opt)
                keep_mask = top_k_mask(keep_score, n_keep)
            else:
                keep_mask = torch.ones_like(mask)

            # determine which weights to grow, if any
            if self.grow_score_f:
                grow_score = self.grow_score_f(param, opt=self.learn.opt)
                # make all keep weights to negative so we don't choose to grow them
                grow_score = grow_score * keep_mask.logical_not() - keep_mask.float()
                grow_mask = top_k_mask(grow_score, n_grow)
            else:
                grow_mask = torch.zeros_like(mask)

            # update network connectivity
            mask.data = keep_mask | grow_mask

            # zero momentum for new connections
            self.reset_momentum(param, grow_mask & keep_mask.logical_not())

    @torch.no_grad()
    def reset_momentum(self, p, mask):
        state = self.opt.state[p]
        if 'grad_avg' in state: state['grad_avg'].mul_(mask)
        if 'sqr_avg' in state: state['sqr_avg'].mul_(mask)

    _docs = dict(__init__='''Args:
    sparse_modules: optional, specify which modules to modify the connectivity of
    batches_per_update: # of batches per update, None (default) updates at end of each training epoch
    initial_drop_grow_pct: percentage of weights to change during each dynamic weight update
    stop_pct: stop dynamic weight updates after `stop_pct` of training
    keep_score_f: function scoring each weight, top n are kept and the rest are zeroed
    grow_score_f: function scoring each weight, top n excl. kept weights are unmasked and initialized to zero''',
                 before_fit="Schedule the number of connections to drop & grow per update.",
                 before_batch="Add dynamic update hooks.",
                 after_backward="Remove dynamic update hooks and skip gradient update.",
                 step="Update self.is_update_step and self.drop_grow_pct.",
                 rewire_module="Update step for one module.",
                 reset_momentum="Initialize momentum to zero for newly-added connections.")

# Cell
SET_presets = {'keep_score_f': weight_magnitude, 'grow_score_f': random_score,
               'initial_drop_grow_pct': 0.3, 'stop_pct': 1.0,}

# Cell
SNFS_presets = {'redistribute_f':momentum_redistribution,
                'keep_score_f': weight_magnitude, 'grow_score_f': gradient_momentum,
                'initial_drop_grow_pct': 0.5, 'stop_pct': 1.0,}

# Cell
RigL_presets = {'keep_score_f': weight_magnitude, 'grow_score_f': gradient_magnitude,
                'initial_drop_grow_pct':0.3, 'stop_pct':0.75, 'batches_per_update': 100}

# Cell
def flop_counter_hook(m, i, o):
    '''Counts FLOPs from nn.Linear and nn.ConvNd layers'''
    flops = 0
    if isinstance(m, (nn.Conv1d, nn.Conv2d, nn.Conv3d)):
        bs,ch,*ks = i[0].shape
        sx, sy = m.stride
        flops = bs * np.prod(ks) * m.weight.numel() / (sx * sy)
    elif isinstance(m, nn.Linear):
        bs = np.prod(i[0].shape[:-1])
        flops = bs * m.weight.numel()
    else:
        return 0
    return flops

def sparse_flop_counter_hook(m, i, o):
    '''Counts FLOPs from nonzero-valued weights.'''
    density = m.weight.abs().gt(0).sum() / m.weight.numel() if hasattr(m, 'weight') else 1
    dense_flops = flop_counter_hook(m, i, o)
    return int(density * dense_flops)

def count_flops(model, xb, sparse=False):
    flops = 0
    hook = sparse_flop_counter_hook if sparse else flop_counter_hook
    with Hooks(flatten_model(model), hook) as h:
        model(xb)
        flops = sum(h.stored)
    return flops

# Cell
class FlopsCounter(HookCallback):
    def __init__(self, sparse=True, verbose=False, **kwargs):
        super().__init__(**kwargs)
        store_attr('sparse,verbose')
    def hook(self, m, i, o):
        f = sparse_flop_counter_hook if self.sparse else flop_counter_hook
        return f(m, i, o)
    def before_fit(self):
        if not hasattr(self, 'm2flops'): self.m2flops = defaultdict(int)
        super().before_fit()
    def after_batch(self):
        "Take the stored results and puts it in `self.m2flops`"
        if self.training and (self.every is None or self.train_iter%self.every == 0):
            for m, flops in zip(self.modules, self.hooks.stored):
                self.m2flops[m] += flops
        super().after_batch()
    def after_fit(self):
        if self.verbose: print(f'Training FLOPs (forward pass only): {self.fwd_train_flops()}')
        super().after_fit()
    def fwd_train_flops(self): return sum(self.m2flops.values())