from __future__ import print_function

import os
from glob import glob
from tqdm import trange
from itertools import chain

import torch
from torch import nn
import torch.nn.parallel
import torchvision.utils as vutils
from torch.autograd import Variable

from qartezator.models import discriminators, generators
from qartezator.data.datamodule import QartezatorDataModule
from qartezator.trainers.discogan.utils import weights_init, generate_with_A, generate_with_B, _get_variable


class Trainer(object):
    def __init__(self, config):
        self.config = config

        self.datamodule = QartezatorDataModule(**config.datamodule)
        hyperparameters = config.hyperparameters
        self.lr = hyperparameters.lr
        self.beta1 = hyperparameters.beta1
        self.beta2 = hyperparameters.beta2
        self.batch_size = hyperparameters.batch_size
        self.weight_decay = hyperparameters.weight_decay

        self.model_dir = config.model_dir
        self.load_path = config.load_path

        self.start_step = 0
        self.log_step = config.log_step
        self.max_step = config.max_step
        self.save_step = config.save_step

        self.build_model()

        if self.load_path:
            self.load_model()

    def build_model(self):
        self.G_AB = generators.DiscoGANGenerator(
                a_channel, b_channel, **self.config.generator).cuda()
        self.G_BA = generators.DiscoGANGenerator(
                b_channel, a_channel, **self.config.generator).cuda()

        self.D_A = discriminators.DiscoGANDiscriminator(
                a_channel, 1, **self.config.discriminator).cuda()
        self.D_B = discriminators.DiscoGANDiscriminator(
                b_channel, 1, **self.config.discriminator).cuda()

        self.G_AB.apply(weights_init)
        self.G_BA.apply(weights_init)

        self.D_A.apply(weights_init)
        self.D_B.apply(weights_init)

    def load_model(self):
        print("[*] Load models from {}...".format(self.load_path))

        paths = glob(os.path.join(self.load_path, 'G_AB_*.pth'))
        paths.sort()

        if len(paths) == 0:
            print("[!] No checkpoint found in {}...".format(self.load_path))
            return

        idxes = [int(os.path.basename(path.split('.')[0].split('_')[-1])) for path in paths]
        self.start_step = max(idxes)

        G_AB_filename = '{}/G_AB_{}.pth'.format(self.load_path, self.start_step)
        self.G_AB.load_state_dict(torch.load(G_AB_filename, map_location=None))
        self.G_BA.load_state_dict(
            torch.load('{}/G_BA_{}.pth'.format(self.load_path, self.start_step), map_location=None))

        self.D_A.load_state_dict(
            torch.load('{}/D_A_{}.pth'.format(self.load_path, self.start_step), map_location=None))
        self.D_B.load_state_dict(
            torch.load('{}/D_B_{}.pth'.format(self.load_path, self.start_step), map_location=None))

        print("[*] Model loaded: {}".format(G_AB_filename))

    def train(self):
        d = nn.MSELoss()
        bce = nn.BCELoss()

        real_label = 1
        fake_label = 0

        real_tensor = Variable(torch.FloatTensor(self.batch_size))
        _ = real_tensor.data.fill_(real_label)

        fake_tensor = Variable(torch.FloatTensor(self.batch_size))
        _ = fake_tensor.data.fill_(fake_label)

        d.cuda()
        bce.cuda()

        real_tensor = real_tensor.cuda()
        fake_tensor = fake_tensor.cuda()

        optimizer_d = torch.optim.Adam(
            chain(self.D_A.parameters(), self.D_B.parameters()),
            lr=self.lr, betas=(self.beta1, self.beta2), weight_decay=self.weight_decay)
        optimizer_g = torch.optim.Adam(
            chain(self.G_AB.parameters(), self.G_BA.parameters()),
            lr=self.lr, betas=(self.beta1, self.beta2))

        train_loader = iter(self.datamodule.train_dataloader())

        for step in trange(self.start_step, self.max_step):
            try:
                x_A, x_B = next(train_loader)
            except StopIteration:
                train_loader = iter(train_dataloader)
                x_A, x_B = next(train_loader)
            if x_A.size(0) != x_B.size(0):
                print("[!] Sampled dataset from A and B have different # of data. Try resampling...")
                continue

            x_A, x_B = _get_variable(x_A), _get_variable(x_B)

            batch_size = x_A.size(0)
            real_tensor.data.resize_(batch_size).fill_(real_label)
            fake_tensor.data.resize_(batch_size).fill_(fake_label)

            # update D network
            self.D_A.zero_grad()
            self.D_B.zero_grad()

            x_AB = self.G_AB(x_A).detach()
            x_BA = self.G_BA(x_B).detach()

            x_ABA = self.G_BA(x_AB).detach()
            x_BAB = self.G_AB(x_BA).detach()

            l_d_A_real, l_d_A_fake = \
                0.5 * torch.mean((self.D_A(x_A) - 1) ** 2), 0.5 * torch.mean((self.D_A(x_BA)) ** 2)
            l_d_B_real, l_d_B_fake = \
                0.5 * torch.mean((self.D_B(x_B) - 1) ** 2), 0.5 * torch.mean((self.D_B(x_AB)) ** 2)

            l_d_A = l_d_A_real + l_d_A_fake
            l_d_B = l_d_B_real + l_d_B_fake

            l_d = l_d_A + l_d_B

            l_d.backward()
            optimizer_d.step()

            # update G network
            self.G_AB.zero_grad()
            self.G_BA.zero_grad()

            x_AB = self.G_AB(x_A)
            x_BA = self.G_BA(x_B)

            x_ABA = self.G_BA(x_AB)
            x_BAB = self.G_AB(x_BA)

            l_const_A = d(x_ABA, x_A)
            l_const_B = d(x_BAB, x_B)

            l_gan_A = 0.5 * torch.mean((self.D_A(x_BA) - 1) ** 2)
            l_gan_B = 0.5 * torch.mean((self.D_B(x_AB) - 1) ** 2)

            l_g = l_gan_A + l_gan_B + l_const_A + l_const_B

            l_g.backward()
            optimizer_g.step()

            if step % self.log_step == 0:
                print("[{}/{}] Loss_D: {:.4f} Loss_G: {:.4f}". \
                      format(step, self.max_step, l_d.data[0], l_g.data[0]))

                print("[{}/{}] l_d_A_real: {:.4f} l_d_A_fake: {:.4f}, l_d_B_real: {:.4f}, l_d_B_fake: {:.4f}". \
                      format(step, self.max_step, l_d_A_real.data[0], l_d_A_fake.data[0],
                             l_d_B_real.data[0], l_d_B_fake.data[0]))

                print("[{}/{}] l_const_A: {:.4f} l_const_B: {:.4f}, l_gan_A: {:.4f}, l_gan_B: {:.4f}". \
                      format(step, self.max_step, l_const_A.data[0], l_const_B.data[0],
                             l_gan_A.data[0], l_gan_B.data[0]))

                generate_with_A(valid_x_A, self.model_dir, self.G_AB, self.G_BA, idx=step)
                generate_with_B(valid_x_B, self.model_dir, self.G_AB, self.G_BA, idx=step)

            if step % self.save_step == self.save_step - 1:
                print("[*] Save models to {}...".format(self.model_dir))

                torch.save(self.G_AB.state_dict(), '{}/G_AB_{}.pth'.format(self.model_dir, step))
                torch.save(self.G_BA.state_dict(), '{}/G_BA_{}.pth'.format(self.model_dir, step))

                torch.save(self.D_A.state_dict(), '{}/D_A_{}.pth'.format(self.model_dir, step))
                torch.save(self.D_B.state_dict(), '{}/D_B_{}.pth'.format(self.model_dir, step))

    def test(self):
        val_loader = iter(self.datamodule.val_dataloader())

        test_dir = os.path.join(self.model_dir, 'test')
        if not os.path.exists(test_dir):
            os.makedirs(test_dir)

        step = 0
        while True:
            try:
                x_A, x_B = val_loader.next()
                x_A, x_B = _get_variable(x_A), _get_variable(x_B)
            except StopIteration:
                print("[!] Test sample generation finished. Samples are in {}".format(test_dir))
                break

            vutils.save_image(x_A.data, '{}/{}_x_A.png'.format(test_dir, step))
            vutils.save_image(x_B.data, '{}/{}_x_B.png'.format(test_dir, step))

            self.generate_with_A(x_A, test_dir, self.G_AB, self.G_BA, idx=step)
            self.generate_with_B(x_B, test_dir, self.G_AB, self.G_BA, idx=step)
            step += 1
