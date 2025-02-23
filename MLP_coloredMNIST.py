import argparse
import numpy as np
import torch
import torch.nn.functional as F
from torchvision import datasets
from torch import nn, optim, autograd
import os

from collections import OrderedDict
from backpack import backpack, extend
from backpack.extensions import BatchGrad

def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")

os.environ["CUDA_VISIBLE_DEVICES"] = '2'
use_cuda = torch.cuda.is_available()

parser = argparse.ArgumentParser(description='Colored MNIST')
parser.add_argument('--hidden_dim', type=int, default=256)
parser.add_argument('--l2_regularizer_weight', type=float,default=0.001)
parser.add_argument('--lr', type=float, default=0.001)
parser.add_argument('--n_restarts', type=int, default=100)
parser.add_argument('--penalty_anneal_iters', type=int, default=100)
parser.add_argument('--penalty_weight', type=float, default=10000.0) #10000
parser.add_argument('--steps', type=int, default=501)
parser.add_argument('--grayscale_model', type=str2bool, default=False)
parser.add_argument('--batch_size', type=int, default=25000)
parser.add_argument('--train_set_size', type=int, default=50000)
parser.add_argument('--eval_interval', type=int, default=100)
parser.add_argument('--print_eval_intervals', type=str2bool, default=True)

parser.add_argument('--train_env_1__color_noise', type=float, default=0.2)
parser.add_argument('--train_env_2__color_noise', type=float, default=0.1)
#parser.add_argument('--val_env__color_noise', type=float, default=0.1)
parser.add_argument('--test_env__color_noise', type=float, default=0.9)

parser.add_argument('--erm_amount', type=float, default=1.0)
parser.add_argument('--noise', type=float, default=1.) #0, 0.5, 1

parser.add_argument('--early_loss_mean', type=str2bool, default=True)

parser.add_argument('--method', type=str, default='icorr') # irmv1, rex, icorr, erm
parser.add_argument('--mse', type=str2bool, default=True)

parser.add_argument('--plot', type=str2bool, default=True)
parser.add_argument('--save_numpy_log', type=str2bool, default=True)

flags = parser.parse_args()

print('Flags:')
for k,v in sorted(vars(flags).items()):
    print("\t{}: {}".format(k, v))

num_batches = (flags.train_set_size // 2) // flags.batch_size

# TODO: logging
all_train_nlls = -1*np.ones((flags.n_restarts, flags.steps))
all_train_accs = -1*np.ones((flags.n_restarts, flags.steps))
#all_train_penalties = -1*np.ones((flags.n_restarts, flags.steps))
all_irmv1_penalties = -1*np.ones((flags.n_restarts, flags.steps))
all_rex_penalties = -1*np.ones((flags.n_restarts, flags.steps))
all_test_accs = -1*np.ones((flags.n_restarts, flags.steps))
all_grayscale_test_accs = -1*np.ones((flags.n_restarts, flags.steps))

final_train_accs = []
final_test_accs = []
highest_test_accs = []
highest_test_accs_5 = []
highest_test_accs_10 = []
for restart in range(flags.n_restarts):
    print("Restart", restart)

    highest_test_acc = 0.0
    highest_test_acc_5 = 0.0
    highest_test_acc_10 = 0.0

    # Load MNIST, make train/val splits, and shuffle train set examples

    mnist = datasets.MNIST('~/datasets/mnist', train=True, download=True)
    mnist_train = (mnist.data[:50000], mnist.targets[:50000])
    mnist_val = (mnist.data[50000:], mnist.targets[50000:])

    rng_state = np.random.get_state()
    np.random.shuffle(mnist_train[0].numpy())
    np.random.set_state(rng_state)
    np.random.shuffle(mnist_train[1].numpy())

  # Build environments

    def make_environment(images, labels, e, sigma, grayscale_dup=False):
        def torch_bernoulli(p, size):
            return (torch.rand(size) < p).float()
        def torch_xor(a, b):
            return (a-b).abs() # Assumes both inputs are either 0 or 1
        # 2x subsample for computational convenience
        images = images.reshape((-1, 28, 28))[:, ::2, ::2]
        # Assign a binary label based on the digit; flip label with probability 0.25
        labels = (labels < 5).float()
        labels = torch_xor(labels, torch_bernoulli(.25, len(labels)))
        # Assign a color based on the label; flip the color with probability e
        colors = torch_xor(labels, torch_bernoulli(e, len(labels)))
        # Apply the color to the image by zeroing out the other color channel
        images = torch.stack([images, images], dim=1)
        if not grayscale_dup:
            images[torch.tensor(range(len(images))), (1-colors).long(), :, :] *= 0
        if sigma>0:
            if use_cuda:
                return {
                'images': (images.float() / 255. + torch.normal(0, sigma, size=images.shape)).cuda(),
                'labels': labels[:, None].cuda()
                }
            else:
                return {
                'images': (images.float() / 255. + torch.normal(0, sigma, size=images.shape)),
                'labels': labels[:, None]
                }
        else:
            if use_cuda:
                return {
                'images': (images.float() / 255.).cuda(),
                'labels': labels[:, None].cuda()
                }
            else:
                return {
                'images': (images.float() / 255.),
                'labels': labels[:, None]
                }

    envs = [
        make_environment(mnist_train[0][::2], mnist_train[1][::2], flags.train_env_1__color_noise, 0.),
        make_environment(mnist_train[0][1::2], mnist_train[1][1::2], flags.train_env_2__color_noise, flags.noise), 
        make_environment(mnist_val[0], mnist_val[1], flags.test_env__color_noise, 0),
        make_environment(mnist_val[0], mnist_val[1], flags.test_env__color_noise, 0.5),
        make_environment(mnist_val[0], mnist_val[1], flags.test_env__color_noise, 1),
    ]  

  # Define and instantiate the model

    class MLP(nn.Module):
        def __init__(self):
            super(MLP, self).__init__()
            if flags.grayscale_model:
                lin1 = nn.Linear(14 * 14, flags.hidden_dim)
            else:
                lin1 = nn.Linear(2 * 14 * 14, flags.hidden_dim)
            lin2 = nn.Linear(flags.hidden_dim, flags.hidden_dim)
            lin3 = nn.Linear(flags.hidden_dim, 1)
            for lin in [lin1, lin2, lin3]:
                nn.init.xavier_uniform_(lin.weight)
                nn.init.zeros_(lin.bias)
            self._main = nn.Sequential(lin1, nn.ReLU(True), lin2, nn.ReLU(True), lin3)
        def forward(self, input):
            if flags.grayscale_model:
                out = input.view(input.shape[0], 2, 14 * 14).sum(dim=1)
            else:
                out = input.view(input.shape[0], 2 * 14 * 14)
            out = self._main(out)
            return out

    if use_cuda:
        mlp = MLP().cuda()
    else:
        mlp = MLP()

  # Define loss function helpers

    def mean_nll(logits, y):
        return nn.functional.binary_cross_entropy_with_logits(logits, y)

    def mean_accuracy(logits, y):
        preds = (logits > 0.).float()
        return ((preds - y).abs() < 1e-2).float().mean()
    
    def corr(logits, y):
        r = logits-torch.mean(logits)
        return torch.sum(y*r)/len(y)

    def penalty(logits, y):
        if use_cuda:
            scale = torch.tensor(1.).cuda().requires_grad_()
        else:
            scale = torch.tensor(1.).requires_grad_()
        loss = mean_nll(logits * scale, y)
        grad = autograd.grad(loss, [scale], create_graph=True)[0]
        return torch.sum(grad**2)
    
    bce_extended = extend(nn.BCEWithLogitsLoss())

  # Train loop

    def pretty_print(*values):
        col_width = 13
        def format_val(v):
            if not isinstance(v, str):
                v = np.array2string(v, precision=5, floatmode='fixed')
            return v.ljust(col_width)
        str_values = [format_val(v) for v in values]
        print("   ".join(str_values))

    optimizer = optim.Adam(mlp.parameters(), lr=flags.lr)

    #pretty_print('step', 'train nll', 'train acc', 'rex penalty', 'irmv1 penalty', 'test acc')

    i = 0
    for step in range(flags.steps):
        n = i % num_batches
        for edx, env in enumerate(envs):
            if edx != len(envs) - 2:
                logits = mlp(env['images'][n*flags.batch_size:(n+1)*flags.batch_size])
                env['nll'] = mean_nll(logits, env['labels'][n*flags.batch_size:(n+1)*flags.batch_size])
                env['acc'] = mean_accuracy(logits, env['labels'][n*flags.batch_size:(n+1)*flags.batch_size])
                env['penalty'] = penalty(logits, env['labels'][n*flags.batch_size:(n+1)*flags.batch_size])
                env['corr'] = corr(logits, env['labels'][n*flags.batch_size:(n+1)*flags.batch_size])
            else:
                logits = mlp(env['images'])
                env['nll'] = mean_nll(logits, env['labels'])
                env['acc'] = mean_accuracy(logits, env['labels'])
                env['penalty'] = penalty(logits, env['labels'])
        i+=1

        train_nll = torch.stack([envs[0]['nll'], envs[1]['nll']]).mean()
        train_acc = torch.stack([envs[0]['acc'], envs[1]['acc']]).mean()
        irmv1_penalty = torch.stack([envs[0]['penalty'], envs[1]['penalty']]).mean()
        icorr_penalty = (envs[0]['corr'] - 0.5*(envs[0]['corr']+envs[1]['corr']))**2
        

        if use_cuda:
            weight_norm = torch.tensor(0.).cuda()
        else:
            weight_norm = torch.tensor(0.)
        for w in mlp.parameters():
            weight_norm += w.norm().pow(2)

        loss1 = envs[0]['nll']
        loss2 = envs[1]['nll']

        if flags.early_loss_mean:
            loss1 = loss1.mean()
            loss2 = loss2.mean()

        loss = 0.0
        loss += flags.erm_amount * (loss1 + loss2)

        loss += flags.l2_regularizer_weight * weight_norm

        penalty_weight = (flags.penalty_weight 
            if step >= flags.penalty_anneal_iters else 1.0)

        if flags.mse:
            rex_penalty = (loss1.mean() - loss2.mean()) ** 2
        else:
            rex_penalty = (loss1.mean() - loss2.mean()).abs()

        if flags.method=='rex':
            loss += penalty_weight * rex_penalty
        elif flags.method=='irmv1':
            loss += penalty_weight * irmv1_penalty
        elif flags.method=='icorr':
            loss += penalty_weight * icorr_penalty

        if penalty_weight > 1.0:
            # Rescale the entire loss to keep gradients in a reasonable range
            loss /= penalty_weight

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        test_acc = envs[2]['acc']
        test_acc_5 = envs[3]['acc']
        test_acc_10 = envs[4]['acc']
        
        
        if step % flags.eval_interval == 0:
            train_acc_scalar = envs[0]['acc'].detach().cpu().numpy()
            test_acc_scalar = test_acc.detach().cpu().numpy()
            test_acc_scalar_5 = test_acc_5.detach().cpu().numpy()
            test_acc_scalar_10 = test_acc_10.detach().cpu().numpy()
            
            '''
            if flags.print_eval_intervals:
                pretty_print(
                  np.int32(step),
                  train_nll.detach().cpu().numpy(),
                  train_acc.detach().cpu().numpy(),
                  rex_penalty.detach().cpu().numpy(),
                  irmv1_penalty.detach().cpu().numpy(),
                  test_acc.detach().cpu().numpy()
                )
            '''
            
            if (train_acc_scalar >= test_acc_scalar) and (test_acc_scalar > highest_test_acc):
                highest_test_acc = test_acc_scalar
                cooresponding_train_acc = train_acc_scalar
                
            if (train_acc_scalar >= test_acc_scalar_5) and (test_acc_scalar_5 > highest_test_acc_5):
                highest_test_acc_5 = test_acc_scalar_5
                cooresponding_train_acc_5 = train_acc_scalar
                
            if (train_acc_scalar >= test_acc_scalar_10) and (test_acc_scalar_10 > highest_test_acc_10):
                highest_test_acc_10 = test_acc_scalar_10
                cooresponding_train_acc_10 = train_acc_scalar
    
    if train_acc_scalar >= test_acc_scalar:
        print('highest test acc this run:', highest_test_acc)
        highest_test_accs.append(highest_test_acc)

        print('highest test acc 0.5 this run:', highest_test_acc_5)
        highest_test_accs_5.append(highest_test_acc_5)

        print('highest test acc 1 this run:', highest_test_acc_10)
        highest_test_accs_10.append(highest_test_acc_10)

highest_test_accs = np.sort(highest_test_accs)
print('max test acc:', np.max(highest_test_accs))
print('min test acc:', np.min(highest_test_accs))
print('mean test acc:', np.mean(highest_test_accs))

highest_test_accs_5 = np.sort(highest_test_accs_5)
print('max test acc 0.5:', np.max(highest_test_accs_5))
print('min test acc 0.5:', np.min(highest_test_accs_5))
print('mean test acc 0.5:', np.mean(highest_test_accs_5))

highest_test_accs_10 = np.sort(highest_test_accs_10)
print('max test acc 1:', np.max(highest_test_accs_10))
print('min test acc 1:', np.min(highest_test_accs_10))
print('mean test acc 1:', np.mean(highest_test_accs_10))