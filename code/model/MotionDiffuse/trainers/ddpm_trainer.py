import torch
import torch.nn.functional as F
import random
import time
import sys
sys.path.append("/home/ltdoanh/jupyter/jupyter/ldtan/NExT-GPT/code/model/MotionDiffuse")
from models.transformer import MotionTransformer
from torch.utils.data import DataLoader
import sys
from datasets1 import build_dataloader
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_
from collections import OrderedDict
sys.path.append("/home/ltdoanh/jupyter/jupyter/ldtan/NExT-GPT/code/model/MotionDiffuse/utils")
from utils.utils import print_current_loss
from os.path import join as pjoin
import codecs as cs
import torch.distributed as dist


from mmengine.dist import get_dist_info,init_dist
from models.gaussian_diffusion import (
    GaussianDiffusion,
    get_named_beta_schedule,
    create_named_schedule_sampler,
    ModelMeanType,
    ModelVarType,
    LossType
)



class DDPMTrainer(object):

    def __init__(self, args, encoder):
        self.opt = args
        self.device = args.device
        self.encoder = encoder
        self.diffusion_steps = args.diffusion_steps
        sampler = 'uniform'
        beta_scheduler = 'linear'
        betas = get_named_beta_schedule(beta_scheduler, self.diffusion_steps)
        self.diffusion = GaussianDiffusion(
            betas=betas,
            model_mean_type=ModelMeanType.EPSILON,
            model_var_type=ModelVarType.FIXED_SMALL,
            loss_type=LossType.MSE
        )
        self.sampler = create_named_schedule_sampler(sampler, self.diffusion)
        self.sampler_name = sampler
        
        if args.is_train:
            self.mse_criterion = torch.nn.MSELoss(reduction='none')
        self.to(self.device)

    @staticmethod
    def zero_grad(opt_list):
        for opt in opt_list:
            opt.zero_grad()

    @staticmethod
    def clip_norm(network_list):
        for network in network_list:
            clip_grad_norm_(network.parameters(), 0.5)

    @staticmethod
    def step(opt_list):
        for opt in opt_list:
            opt.step()

    def forward(self, batch_data, eval_mode=False):
        caption, motions, m_lens = batch_data
        motions = motions.detach().to(self.device).float()

        self.caption = caption
        self.motions = motions
        x_start = motions
        B, T = x_start.shape[:2]
        cur_len = torch.LongTensor([min(T, m_len) for m_len in  m_lens]).to(self.device)
        t, _ = self.sampler.sample(B, x_start.device)
        output = self.diffusion.training_losses(
            model=self.encoder,
            x_start=x_start,
            t=t,
            model_kwargs={"text": caption, "length": cur_len}
        )

        self.real_noise = output['target']
        self.fake_noise = output['pred']
        try:
            self.src_mask = self.encoder.module.generate_src_mask(T, cur_len).to(x_start.device)
        except:
            self.src_mask = self.encoder.generate_src_mask(T, cur_len).to(x_start.device)

    def generate_batch(self, caption, m_lens, dim_pose, device, promt = None):
        xf_proj, xf_out = self.encoder.encode_text(caption, device)
        # if promt != None:
        text_promts_embed, text_promts_out = self.encoder.encode_promt(promt, self.device)
        # print(xf_proj.shape)
        # print(xf_out.shape)
        # print(text_promts_embed.shape)
        # print(text_promts_out.shape)
        B = len(promt)
        T = min(m_lens.max(), self.encoder.num_frames)
        # T = self.encoder.num_frames 
        output = self.diffusion.p_sample_loop(
            self.encoder,
            (B, T, dim_pose),
            clip_denoised=False,
            progress=True,
            text_promts_out = text_promts_out,
            text_promts_embed =  text_promts_embed,
            model_kwargs={
                'xf_proj': xf_proj,
                'xf_out': xf_out,
                'length': m_lens
            })
        return output

    # def generate(self, caption, m_lens, dim_pose, batch_size=1024, device='cuda:0'):
    #     N = len(caption)
    #     cur_idx = 0
    #     self.encoder.eval()
    #     all_output = []
    #     while cur_idx < N:
    #         if cur_idx + batch_size >= N:
    #             batch_caption = caption[cur_idx:]
    #             batch_m_lens = m_lens[cur_idx:]
    #         else:
    #             batch_caption = caption[cur_idx: cur_idx + batch_size]
    #             batch_m_lens = m_lens[cur_idx: cur_idx + batch_size]
    #         output = self.generate_batch(batch_caption, batch_m_lens, dim_pose,device)
    #         B = output.shape[0]

    #         for i in range(B):
    #             all_output.append(output[i])
    #         cur_idx += batch_size
    #     return all_output
    def generate(self, caption, m_lens, dim_pose,promt, batch_size=1024, device='cuda:0'):
        N = len(caption)
        cur_idx = 0
        self.encoder.eval()
        all_output = []

        output = self.generate_batch(caption, m_lens, dim_pose, promt = promt, device=device)
        B = output.shape[0]

        for i in range(B):
            all_output.append(output[i])
        # cur_idx += batch_size

        return all_output

    def backward_G(self, a, b ,src_mask):
    # def backward_G(self):
        # loss_mot_rec = self.mse_criterion(self.fake_noise, self.real_noise).mean(dim=-1)
        if torch.isnan(a).any() or torch.isinf(a).any():
            a = torch.zeros_like(a)
        if torch.isnan(b).any() or torch.isinf(b).any():
            b = torch.zeros_like(b)
        loss_mot_rec = self.mse_criterion(a, b).mean(dim=-1)
        # Replace NaN values with zero
        loss_mot_rec = torch.where(torch.isnan(loss_mot_rec), torch.tensor(0.0, device=loss_mot_rec.device), loss_mot_rec)
        # loss_mot_rec = (loss_mot_rec * self.src_mask).sum() / self.src_mask.sum()
        if torch.isnan(src_mask).any() or torch.isinf(src_mask).any():
            src_mask.sum() == 1
        loss_mot_rec = (loss_mot_rec * src_mask).sum() / (src_mask.sum() + 1e-8)
        loss_mot_rec = torch.where(torch.isnan(loss_mot_rec), torch.tensor(0.0, device=loss_mot_rec.device), loss_mot_rec)
        self.loss_mot_rec = loss_mot_rec
        loss_logs = OrderedDict({})
        loss_logs['loss_mot_rec'] = self.loss_mot_rec.item()
        return loss_logs, loss_mot_rec

    def update(self):
        self.zero_grad([self.opt_encoder])
        loss_logs = self.backward_G()
        self.loss_mot_rec.backward()
        self.clip_norm([self.encoder])
        self.step([self.opt_encoder])

        return loss_logs

    def to(self, device):
        if self.opt.is_train:
            self.mse_criterion.to(device)
        self.encoder = self.encoder.to(device)

    def train_mode(self):
        self.encoder.train()

    def eval_mode(self):
        self.encoder.eval()

    def save(self, file_name, ep, total_it,opt_encoder):
        state = {
            # 'opt_encoder': self.opt_encoder.state_dict(),
            'opt_encoder': opt_encoder.state_dict(),
            'ep': ep,
            'total_it': total_it
        }
        try:
            state['encoder'] = self.encoder.module.state_dict()
        except:
            state['encoder'] = self.encoder.state_dict()
        torch.save(state, file_name)
        return

    def load(self, model_dir):
        # checkpoint = torch.load(model_dir, map_location=self.device)
        checkpoint = torch.load(model_dir, map_location='cuda')
        if self.opt.is_train:
            self.opt_encoder.load_state_dict(checkpoint['opt_encoder'])
        
        self.encoder.load_state_dict(checkpoint['encoder'], strict=False)
  
        return checkpoint['ep'], checkpoint.get('total_it', 0)

    def train(self, train_dataset):
        rank, world_size = get_dist_info()
        self.to(self.device)
        self.opt_encoder = optim.Adam(self.encoder.parameters(), lr=self.opt.lr)
        it = 0
        cur_epoch = 0
        if self.opt.is_continue:
            # model_dir = pjoin(self.opt.model_dir, 'latest.tar')
            model_dir = '/home/ltdoanh/jupyter/jupyter/ldtan/NExT-GPT/code/model/MotionDiffuse/checkpoints/t2m/t2m_motiondiffuse/model/latest.tar'
            cur_epoch, it = self.load(model_dir)
            print('doanh')

        start_time = time.time()

        train_loader = build_dataloader(
            train_dataset,
            samples_per_gpu=self.opt.batch_size,
            drop_last=True,
            workers_per_gpu=4,
            shuffle=True)

        logs = OrderedDict()
        for epoch in range(cur_epoch, self.opt.num_epochs):
            self.train_mode()
            for i, batch_data in enumerate(train_loader):
                self.forward(batch_data)
                log_dict = self.update()
                for k, v in log_dict.items():
                    if k not in logs:
                        logs[k] = v
                    else:
                        logs[k] += v
                it += 1
                if it % self.opt.log_every == 0 and rank == 0:
                    mean_loss = OrderedDict({})
                    for tag, value in logs.items():
                        mean_loss[tag] = value / self.opt.log_every
                    logs = OrderedDict()
                    print_current_loss(start_time, it, mean_loss, epoch, inner_iter=i)
                # self.save(pjoin('/home/ltdoanh/jupyter/jupyter/ldtan/NExT-GPT', 'latest123.tar'), epoch, it)
                # if it % self.opt.save_latest == 0 and rank == 0:
                #     self.save(pjoin('/home/ltdoanh/jupyter/jupyter/ldtan/NExT-GPT', 'latest.tar'), epoch, it)

            # if rank == 0:
            self.save(pjoin('/home/ltdoanh/jupyter/jupyter/ldtan/NExT-GPT', 'latest1234.tar'), epoch, it)

            if epoch % self.opt.save_every_e == 0 and rank == 0:
                self.save(pjoin(self.opt.model_dir, 'ckpt_e%03d.tar'%(epoch)),
                            epoch, total_it=it)
if __name__ == '__main__':
    input = torch.rand(1,77,768)
    doanh = DDPMTrainer()