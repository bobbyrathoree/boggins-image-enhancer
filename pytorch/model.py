import logging
from collections import OrderedDict

import torch
import torch.nn as nn
from torch.optim import lr_scheduler

from networks import *
from borrowed.loss import GANLoss, GradientPenaltyLoss

logger = logging.getLogger("base")


class BaseModel:
    def __init__(self, opt):
        self.opt = opt
        self.device = torch.device("cuda" if opt["gpu_ids"] is not None else "cpu")
        self.is_train = opt["is_train"]
        self.schedulers = []
        self.optimizers = []

    def feed_data(self, data):
        pass

    def optimize_parameters(self):
        pass

    def get_current_visuals(self):
        pass

    def get_current_losses(self):
        pass

    def print_network(self):
        pass

    def save(self, label):
        pass

    def load(self):
        pass

    def update_learning_rate(self):
        for scheduler in self.schedulers:
            scheduler.step()

    def get_current_learning_rate(self):
        return self.schedulers[0].get_lr()[0]

    def get_network_description(self, network):
        if isinstance(network, nn.DataParallel):
            network = network.module
        s = str(network)
        n = sum(map(lambda x: x.numel(), network.parameters()))
        return s, n

    def save_network(self, network, network_label, iter_step):
        save_filename = "{}_{}.pth".format(iter_step, network_label)
        save_path = os.path.join(self.opt["path"]["models"], save_filename)
        if isinstance(network, nn.DataParallel):
            network = network.module
        state_dict = network.state_dict()
        for key, param in state_dict.items():
            state_dict[key] = param.cpu()
        torch.save(state_dict, save_path)

    def load_network(self, load_path, network, strict=True):
        if isinstance(network, nn.DataParallel):
            network = network.module
        network.load_state_dict(torch.load(load_path), strict=strict)

    def save_training_state(self, epoch, iter_step):
        state = {"epoch": epoch, "iter": iter_step, "schedulers": [], "optimizers": []}
        for s in self.schedulers:
            state["schedulers"].append(s.state_dict())
        for o in self.optimizers:
            state["optimizers"].append(o.state_dict())
        save_filename = "{}.state".format(iter_step)
        save_path = os.path.join(self.opt["path"]["training_state"], save_filename)
        torch.save(state, save_path)

    def resume_training(self, resume_state):
        resume_optimizers = resume_state["optimizers"]
        resume_schedulers = resume_state["schedulers"]
        assert len(resume_optimizers) == len(
            self.optimizers
        ), "Wrong lengths of optimizers"
        assert len(resume_schedulers) == len(
            self.schedulers
        ), "Wrong lengths of schedulers"
        for i, o in enumerate(resume_optimizers):
            self.optimizers[i].load_state_dict(o)
        for i, s in enumerate(resume_schedulers):
            self.schedulers[i].load_state_dict(s)


class SRGANModel(BaseModel):
    def __init__(self, opt):
        super(SRGANModel, self).__init__(opt)
        train_opt = opt["train"]

        self.netG = networks.define_G(opt).to(self.device)
        if self.is_train:
            self.netD = networks.define_D(opt).to(self.device)
            self.netG.train()
            self.netD.train()
        self.load()

        if self.is_train:
            if train_opt["pixel_weight"] > 0:
                l_pix_type = train_opt["pixel_criterion"]
                if l_pix_type == "l1":
                    self.cri_pix = nn.L1Loss().to(self.device)
                elif l_pix_type == "l2":
                    self.cri_pix = nn.MSELoss().to(self.device)
                else:
                    raise NotImplementedError(
                        "Loss type [{:s}] not recognized.".format(l_pix_type)
                    )
                self.l_pix_w = train_opt["pixel_weight"]
            else:
                logger.info("Remove pixel loss.")
                self.cri_pix = None

            if train_opt["feature_weight"] > 0:
                l_fea_type = train_opt["feature_criterion"]
                if l_fea_type == "l1":
                    self.cri_fea = nn.L1Loss().to(self.device)
                elif l_fea_type == "l2":
                    self.cri_fea = nn.MSELoss().to(self.device)
                else:
                    raise NotImplementedError(
                        "Loss type [{:s}] not recognized.".format(l_fea_type)
                    )
                self.l_fea_w = train_opt["feature_weight"]
            else:
                logger.info("Remove feature loss.")
                self.cri_fea = None
            if self.cri_fea:
                self.netF = networks.define_F(opt, use_bn=False).to(self.device)

            self.cri_gan = GANLoss(train_opt["gan_type"], 1.0, 0.0).to(self.device)
            self.l_gan_w = train_opt["gan_weight"]
            self.D_update_ratio = (
                train_opt["D_update_ratio"] if train_opt["D_update_ratio"] else 1
            )
            self.D_init_iters = (
                train_opt["D_init_iters"] if train_opt["D_init_iters"] else 0
            )

            if train_opt["gan_type"] == "wgan-gp":
                self.random_pt = torch.Tensor(1, 1, 1, 1).to(self.device)
                self.cri_gp = GradientPenaltyLoss(device=self.device).to(self.device)
                self.l_gp_w = train_opt["gp_weigth"]

            wd_G = train_opt["weight_decay_G"] if train_opt["weight_decay_G"] else 0
            optim_params = []
            for (k, v) in self.netG.named_parameters():
                if v.requires_grad:
                    optim_params.append(v)
                else:
                    logger.warning("Params [{:s}] will not optimize.".format(k))
            self.optimizer_G = torch.optim.Adam(
                optim_params,
                lr=train_opt["lr_G"],
                weight_decay=wd_G,
                betas=(train_opt["beta1_G"], 0.999),
            )
            self.optimizers.append(self.optimizer_G)
            wd_D = train_opt["weight_decay_D"] if train_opt["weight_decay_D"] else 0
            self.optimizer_D = torch.optim.Adam(
                self.netD.parameters(),
                lr=train_opt["lr_D"],
                weight_decay=wd_D,
                betas=(train_opt["beta1_D"], 0.999),
            )
            self.optimizers.append(self.optimizer_D)

            if train_opt["lr_scheme"] == "MultiStepLR":
                for optimizer in self.optimizers:
                    self.schedulers.append(
                        lr_scheduler.MultiStepLR(
                            optimizer, train_opt["lr_steps"], train_opt["lr_gamma"]
                        )
                    )
            else:
                raise NotImplementedError("MultiStepLR learning rate scheme is enough.")

            self.log_dict = OrderedDict()
        self.print_network()

    def feed_data(self, data, need_HR=True):
        self.var_L = data["LR"].to(self.device)
        if need_HR:
            self.var_H = data["HR"].to(self.device)

            input_ref = data["ref"] if "ref" in data else data["HR"]
            self.var_ref = input_ref.to(self.device)

    def optimize_parameters(self, step):
        self.optimizer_G.zero_grad()
        self.fake_H = self.netG(self.var_L)

        l_g_total = 0
        if step % self.D_update_ratio == 0 and step > self.D_init_iters:
            if self.cri_pix:
                l_g_pix = self.l_pix_w * self.cri_pix(self.fake_H, self.var_H)
                l_g_total += l_g_pix
            if self.cri_fea:
                real_fea = self.netF(self.var_H).detach()
                fake_fea = self.netF(self.fake_H)
                l_g_fea = self.l_fea_w * self.cri_fea(fake_fea, real_fea)
                l_g_total += l_g_fea
            pred_g_fake = self.netD(self.fake_H)
            l_g_gan = self.l_gan_w * self.cri_gan(pred_g_fake, True)
            l_g_total += l_g_gan

            l_g_total.backward()
            self.optimizer_G.step()

        self.optimizer_D.zero_grad()
        l_d_total = 0
        pred_d_real = self.netD(self.var_ref)
        l_d_real = self.cri_gan(pred_d_real, True)
        pred_d_fake = self.netD(self.fake_H.detach())
        l_d_fake = self.cri_gan(pred_d_fake, False)

        l_d_total = l_d_real + l_d_fake

        if self.opt["train"]["gan_type"] == "wgan-gp":
            batch_size = self.var_ref.size(0)
            if self.random_pt.size(0) != batch_size:
                self.random_pt.resize_(batch_size, 1, 1, 1)
            self.random_pt.uniform_()
            interp = (
                self.random_pt * self.fake_H.detach()
                + (1 - self.random_pt) * self.var_ref
            )
            interp.requires_grad = True
            interp_crit = self.netD(interp)
            l_d_gp = self.l_gp_w * self.cri_gp(interp, interp_crit)
            l_d_total += l_d_gp

        l_d_total.backward()
        self.optimizer_D.step()

        if step % self.D_update_ratio == 0 and step > self.D_init_iters:
            if self.cri_pix:
                self.log_dict["l_g_pix"] = l_g_pix.item()
            if self.cri_fea:
                self.log_dict["l_g_fea"] = l_g_fea.item()
            self.log_dict["l_g_gan"] = l_g_gan.item()
        self.log_dict["l_d_real"] = l_d_real.item()
        self.log_dict["l_d_fake"] = l_d_fake.item()

        if self.opt["train"]["gan_type"] == "wgan-gp":
            self.log_dict["l_d_gp"] = l_d_gp.item()
        self.log_dict["D_real"] = torch.mean(pred_d_real.detach())
        self.log_dict["D_fake"] = torch.mean(pred_d_fake.detach())

    def test(self):
        self.netG.eval()
        with torch.no_grad():
            self.fake_H = self.netG(self.var_L)
        self.netG.train()

    def get_current_log(self):
        return self.log_dict

    def get_current_visuals(self, need_HR=True):
        out_dict = OrderedDict()
        out_dict["LR"] = self.var_L.detach()[0].float().cpu()
        out_dict["SR"] = self.fake_H.detach()[0].float().cpu()
        if need_HR:
            out_dict["HR"] = self.var_H.detach()[0].float().cpu()
        return out_dict

    def print_network(self):
        s, n = self.get_network_description(self.netG)
        if isinstance(self.netG, nn.DataParallel):
            net_struc_str = "{} - {}".format(
                self.netG.__class__.__name__, self.netG.module.__class__.__name__
            )
        else:
            net_struc_str = "{}".format(self.netG.__class__.__name__)
        logger.info(
            "Network G structure: {}, with parameters: {:,d}".format(net_struc_str, n)
        )
        logger.info(s)
        if self.is_train:
            s, n = self.get_network_description(self.netD)
            if isinstance(self.netD, nn.DataParallel):
                net_struc_str = "{} - {}".format(
                    self.netD.__class__.__name__, self.netD.module.__class__.__name__
                )
            else:
                net_struc_str = "{}".format(self.netD.__class__.__name__)
            logger.info(
                "Network D structure: {}, with parameters: {:,d}".format(
                    net_struc_str, n
                )
            )
            logger.info(s)

            if self.cri_fea:
                s, n = self.get_network_description(self.netF)
                if isinstance(self.netF, nn.DataParallel):
                    net_struc_str = "{} - {}".format(
                        self.netF.__class__.__name__,
                        self.netF.module.__class__.__name__,
                    )
                else:
                    net_struc_str = "{}".format(self.netF.__class__.__name__)
                logger.info(
                    "Network F structure: {}, with parameters: {:,d}".format(
                        net_struc_str, n
                    )
                )
                logger.info(s)

    def load(self):
        load_path_G = self.opt["path"]["pretrain_model_G"]
        if load_path_G is not None:
            logger.info("Loading pretrained model for G [{:s}] ...".format(load_path_G))
            self.load_network(load_path_G, self.netG)
        load_path_D = self.opt["path"]["pretrain_model_D"]
        if self.opt["is_train"] and load_path_D is not None:
            logger.info("Loading pretrained model for D [{:s}] ...".format(load_path_D))
            self.load_network(load_path_D, self.netD)

    def save(self, iter_step):
        self.save_network(self.netG, "G", iter_step)
        self.save_network(self.netD, "D", iter_step)
