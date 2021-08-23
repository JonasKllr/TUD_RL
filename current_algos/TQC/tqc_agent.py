import numpy as np
import copy
import torch
import math
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F

from current_algos.TQC.tqc_buffer import UniformReplayBuffer
from current_algos.TQC.tqc_nets import Actor, Critic
from current_algos.common.normalizer import Action_Normalizer, Input_Normalizer
from current_algos.common.logging_func import *

# Implement TQC Agent after Kuznetsov et al. 2020
# This is a minimal implementation. No gradient rescaling, target network
# updates on every step, automatic temperature tuning.
class TQC_Agent:
    def __init__(self, 
    mode,
    action_dim, 
    state_dim,
    action_high,
    action_low,
    top_quantiles_to_drop,
    n_quantiles = 25, # Number of atoms per critic // Quantiles estimates per critic
    n_critics = 4, # Number of critics with n_quantiles of atoms each.
    actor_weights = None,
    critic_weights = None,
    input_norm = False,
    input_norm_prior = None, 
    gamma = 0.99,
    polyak_tau = 0.005,
    lr_actor = 0.0003,
    lr_critic = 0.0003,
    buffer_length = 1000000,
    batch_size = 128,
    temperature = 0.2,
    device = "cpu",
     ) -> None:
        
        # store attributes and hyperparameters
        assert mode in ["train", "test"], "Unknown mode. Should be 'train' or 'test'."
        assert not (mode == "test" and (actor_weights is None or critic_weights is None)), "Need prior weights in test mode."
        self.mode             = mode
        
        self.name             = "TQC_Agent"
        self.action_dim       = action_dim
        self.state_dim        = state_dim
        self.action_high      = action_high
        self.action_low       = action_low
        self.actor_weights    = actor_weights
        self.critic_weights   = critic_weights 
        self.input_norm       = input_norm
        self.input_norm_prior = input_norm_prior
        self.gamma            = gamma
        self.polyak_tau       = polyak_tau
        self.lr_actor         = lr_actor
        self.lr_critic        = lr_critic
        self.buffer_length    = buffer_length
        self.batch_size       = batch_size
        self.target_entropy   = -action_dim
        self.top_quantiles_to_drop = top_quantiles_to_drop
        self.log_alpha        = torch.zeros((1,),requires_grad=True,device=device)

        assert device in ["cpu","cuda"], "Unknown device"
        print("Using GPU") if device == "cuda" else print("Using CPU")
        self.device           = device

        # Logging
        self.logger = EpochLogger()
        self.logger.save_config(locals())

        # Init replay buffer
        self.replay_buffer = UniformReplayBuffer(state_dim, action_dim, buffer_length, device)

        # Action Normalizer
        self.action_normalizer = Action_Normalizer(action_high, action_low)

        # Init Actor and Quantile Critics
        self.actor = Actor(action_dim, state_dim)
        self.critic = Critic(state_dim,action_dim,n_quantiles,n_critics)

        # Calculate the total number of quantiles to be used
        self.total_quantiles = self.critic.n_critics * self.critic.n_quantiles

        # load prior weights if available
        if actor_weights is not None and critic_weights is not None:
            self.actor.load_state_dict(torch.load(actor_weights))
            self.critic.load_state_dict(torch.load(critic_weights))

        # Init target networks
        self.critic_target = copy.deepcopy(self.critic).to(self.device)

        # Define optimizers for actor, critics and temerpature
        self.actor_optim = optim.Adam(self.actor.parameters(),self.lr_actor)
        self.critic_optim = optim.Adam(self.critic.parameters(),self.lr_critic)
        self.temp_optim = optim.Adam([self.log_alpha],self.lr_critic)
        
    
    @torch.no_grad()
    def select_action(self,s):

        # reshape obs
        s = torch.tensor(s.astype(np.float32)).view(1, self.state_dim).to(self.device)

        a,_ = self.actor(s)

        # reshape actions
        a = a.cpu().numpy().reshape(self.action_dim)
        
        # transform [-1,1] to application scale
        return self.action_normalizer.norm_to_action(a)
    
    def memorize(self, s, a, r, s2, d):

        a = self.action_normalizer.action_to_norm(a)
        self.replay_buffer.add(s,a,r,s2,d)

    def train(self):

        batch = self.replay_buffer.sample(self.batch_size)

        s,a,r,s2,d = batch

        # retrieve alpha
        alpha = torch.exp(self.log_alpha)

        with torch.no_grad():
            next_new_action, next_log_pi = self.actor(s2)

            # Compute the quantiles for building the target
            # distribution and cut the n topmost quantiles
            next_z = self.critic_target(s2,next_new_action) # Eq. (10) Kuznetsov et al. 2020

            # Sort the calculated quantile atoms in ascending order
            sorted_z, _ = torch.sort(next_z.reshape(self.batch_size, -1))

            # Cut the top n quantiles
            sorted_z_part = sorted_z[:,:self.total_quantiles - self.top_quantiles_to_drop] #Eq. 11

            # Calculate the individual targets from the target distribution
            target = r + (1-d) * self.gamma * (sorted_z_part - alpha * next_log_pi) # Eq. 12

        current_z = self.critic(s,a)
        self.logger.store(Avg_Q_val = current_z.mean().detach().cpu().numpy().item())

        critic_loss = self.quantile_huber_loss(current_z,target)

        # Policy and alpha losses
        new_action, log_pi = self.actor(s)
        alpha_loss = -self.log_alpha * (log_pi + self.target_entropy).detach().mean() # Eq. 5
        current_mean_z = self.critic(s,new_action).mean(2).mean(1,keepdim=True)
        actor_loss = (alpha*log_pi - current_mean_z).mean()

        # SGD
        self.temp_optim.zero_grad()
        alpha_loss.backward()
        self.temp_optim.step()

        self.actor_optim.zero_grad()
        actor_loss.backward()
        self.actor_optim.step()

        self.critic_optim.zero_grad()
        critic_loss.backward()
        self.critic_optim.step()

        # Log critic loss
        self.logger.store(Critic_loss=critic_loss.detach().cpu().numpy().item())

        # Polyak update
        for param, target_param in zip(self.critic.parameters(), self.critic_target.parameters()):
            target_param.data.copy_(self.polyak_tau * param.data + (1 - self.polyak_tau) * target_param.data)

        # Log actor loss
        self.logger.store(Actor_loss=actor_loss.detach().cpu().numpy().item())


    # Compute the quantile Huber loss to approximate the 1-Wasserstein distance between quantiles
    def quantile_huber_loss(self,quantiles, targets):

        pairwise_delta = targets[:,None,None,:] - quantiles[:,:,:,None] # Reshape to
        abs_pairwise_delta = torch.abs(pairwise_delta)

        # Compute huber loss as in Dabney, 2018
        huber_loss = torch.where(abs_pairwise_delta >1,
                                abs_pairwise_delta - 0.5,
                                pairwise_delta * 0.5 ** 2)

        n_quantiles = quantiles.shape[2]
        tau = torch.arange(n_quantiles, device=self.device).float() / n_quantiles + 1/2 / n_quantiles
        loss = (torch.abs(tau[None,None,:,None] - (pairwise_delta < 0).float()) * huber_loss).mean()
        return loss