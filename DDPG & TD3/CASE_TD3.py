import copy

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions.multivariate_normal import MultivariateNormal
from torch.distributions.kl import kl_divergence

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# Implementation of the Twin Delayed Deep Deterministic Policy Gradient algorithm (TD3)
# Paper: https://arxiv.org/abs/1802.09477
# Note: This implementation heavily relies on the author's PyTorch implementation of TD3.
# Repository: https://github.com/sfujim/TD3

class Actor(nn.Module):
    def __init__(self, state_dim, action_dim, max_action):
        super(Actor, self).__init__()

        self.l1 = nn.Linear(state_dim, 256)
        self.l2 = nn.Linear(256, 256)
        self.l3 = nn.Linear(256, action_dim)

        self.max_action = max_action

    def forward(self, state):
        a = F.relu(self.l1(state))
        a = F.relu(self.l2(a))
        return self.max_action * torch.tanh(self.l3(a))


class Critic(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Critic, self).__init__()

        # Q1 architecture
        self.l1 = nn.Linear(state_dim + action_dim, 256)
        self.l2 = nn.Linear(256, 256)
        self.l3 = nn.Linear(256, 1)

        # Q2 architecture
        self.l4 = nn.Linear(state_dim + action_dim, 256)
        self.l5 = nn.Linear(256, 256)
        self.l6 = nn.Linear(256, 1)

    def forward(self, state, action):
        sa = torch.cat([state, action], 1)

        q1 = F.relu(self.l1(sa))
        q1 = F.relu(self.l2(q1))
        q1 = self.l3(q1)

        q2 = F.relu(self.l4(sa))
        q2 = F.relu(self.l5(q2))
        q2 = self.l6(q2)
        return q1, q2

    def Q1(self, state, action):
        sa = torch.cat([state, action], 1)

        q1 = F.relu(self.l1(sa))
        q1 = F.relu(self.l2(q1))
        q1 = self.l3(q1)
        return q1


class CASE_TD3(object):
    def __init__(
            self,
            state_dim,
            action_dim,
            max_action,
            agent_id,
            gpu,
            discount=0.99,
            tau=0.005,
            policy_noise=0.2,
            noise_clip=0.5,
            policy_freq=2,
            kl_div_var=0.15
    ):
        self.device = torch.device(f"cuda:{gpu}" if torch.cuda.is_available() else "cpu")

        # Initialize actor networks and optimizer
        self.actor = Actor(state_dim, action_dim, max_action).to(device)
        self.actor_target = copy.deepcopy(self.actor)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=3e-4)

        # Initialize critic networks and optimizer
        self.critic = Critic(state_dim, action_dim).to(device)
        self.critic_target = copy.deepcopy(self.critic)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=3e-4)

        # Initialize the training parameters
        self.max_action = max_action
        self.discount = discount
        self.tau = tau
        self.policy_noise = policy_noise
        self.noise_clip = noise_clip
        self.policy_freq = policy_freq

        self.total_it = 0

        self.action_dim = action_dim

        # Determine the agent ID and initialize the reference Gaussian
        self.agent_id = agent_id
        self.kl_div_var = kl_div_var

        self.ref_gaussian = MultivariateNormal(torch.zeros(self.action_dim).to(self.device),
                                               torch.eye(self.action_dim).to(self.device) * self.kl_div_var)

    def select_action(self, state):
        state = torch.FloatTensor(state.reshape(1, -1)).to(device)
        return self.actor(state).cpu().data.numpy().flatten()

    def update_parameters(self, replay_buffer, batch_size=256):
        self.total_it += 1

        # Sample from the experience replay buffer
        state, action, next_state, reward, agent_id, not_done = replay_buffer.sample(batch_size)

        with torch.no_grad():
            # Select action according to the target policy and add target smoothing regularization
            noise = (torch.randn_like(action) * self.policy_noise).clamp(-self.noise_clip, self.noise_clip)
            next_action = (self.actor_target(next_state) + noise).clamp(-self.max_action, self.max_action)

            # Compute the target Q-value
            target_Q1, target_Q2 = self.critic_target(next_state, next_action)
            target_Q = torch.min(target_Q1, target_Q2)
            target_Q = reward + not_done * self.discount * target_Q

        # Get the current Q-value estimates
        current_Q1, current_Q2 = self.critic(state, action)

        # Obtain the indices corresponding to the internal and external experiences
        ext_idx, _ = torch.where(agent_id != self.agent_id)
        self_idx, _ = torch.where(agent_id == self.agent_id)

        kl_weights = torch.ones_like(reward)

        try:
            # Get current actions on the states of external experiences
            with torch.no_grad():
                current_action = self.actor(state[ext_idx])

            # Compute the difference batch
            diff_action_batch = action[ext_idx] - current_action

            # Get the mean and covariance matrix for the
            mean = torch.mean(diff_action_batch, dim=0)
            cov = torch.mm(torch.transpose(diff_action_batch - mean, 0, 1), diff_action_batch - mean) / len(ext_idx)

            multivar_gaussian = MultivariateNormal(mean, cov)

            kl_div = (kl_divergence(multivar_gaussian, self.ref_gaussian) + kl_divergence(self.ref_gaussian,
                                                                                          multivar_gaussian)) / 2

            kl_div = torch.exp(-kl_div)

            kl_weights[ext_idx] = kl_div
        except:
            kl_weights[ext_idx] = 0

        # Compute critic loss
        critic_loss = torch.sum(kl_weights * (F.mse_loss(current_Q1, target_Q, reduction='none') +
                                              F.mse_loss(current_Q2, target_Q, reduction='none'))) / torch.sum(kl_weights)

        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

        # Delayed policy updates, update actor networks every update period
        if self.total_it % self.policy_freq == 0:

            # Compute the actor loss
            actor_loss = -(kl_weights * self.critic.Q1(state, self.actor(state))).sum() / torch.sum(kl_weights)

            # Optimize the actor
            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

            # Soft update the target networks
            for param, target_param in zip(self.critic.parameters(), self.critic_target.parameters()):
                target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

            for param, target_param in zip(self.actor.parameters(), self.actor_target.parameters()):
                target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

    # Save the model parameters
    def save(self, file_name):
        torch.save(self.actor.state_dict(), file_name + "_actor")
        torch.save(self.actor_optimizer.state_dict(), file_name + "_actor_optimizer")

        torch.save(self.critic.state_dict(), file_name + "_critic")
        torch.save(self.critic_optimizer.state_dict(), file_name + "_critic_optimizer")

    # Load the model parameters
    def load(self, filename):
        self.actor.load_state_dict(torch.load(filename + "_actor"))
        self.actor_optimizer.load_state_dict(torch.load(filename + "_actor_optimizer"))
        self.actor_target = copy.deepcopy(self.actor)

        self.critic.load_state_dict(torch.load(filename + "_critic"))
        self.critic_optimizer.load_state_dict(torch.load(filename + "_critic_optimizer"))
        self.critic_target = copy.deepcopy(self.critic)


