# @Time   : 2020/12/1
# @Author : Xiaolei Wang
# @Email  : wxl1999@foxmail.com

# UPDATE:
# @Time   : 2020/12/1
# @Author : Xiaolei Wang
# @Email  : wxl1999@foxmail.com

from abc import abstractmethod, ABC

from loguru import logger
from torch import optim
import numpy as np


class LRScheduler(ABC):
    """
    Class for LR Schedulers.

    Includes some basic functionality by default - setting up the warmup
    scheduler, passing the correct number of steps to train_step, loading and
    saving states.
    Subclasses must implement abstract methods train_step() and valid_step().
    Schedulers should be initialized with lr_scheduler_factory().
    __init__() should not be called directly.
    """

    def __init__(self, hard_reset, warmup_updates, warmup_rate):
        """
        Initialize warmup scheduler. Specific main schedulers should be initialized in
        the subclasses. Do not invoke this method diretly.

        :param optimizer optimizer:
            Optimizer being used for training. May be wrapped in
            fp16_optimizer_wrapper depending on whether fp16 is used.
        :param state_dict states:
            Possible state_dict provided by model checkpoint, for restoring
            LR state.
        :param bool hard_reset:
            If true, the LR scheduler should ignore the state dictionary.
        :param int warmup_updates:
            Number of training step updates warmup scheduler should take.
        :param float warmup_rate:
            Starting multiplier for warmup scheduler.
        """
        self._number_training_updates = 0
        self.warmup_updates = warmup_updates
        self.warmup_rate = warmup_rate
        self.hard_reset = hard_reset

    def _init_warmup_scheduler(self, optimizer, states):
        updates_so_far = states.get('number_training_updates', 0)
        if self.warmup_updates > 0 and (
            updates_so_far < self.warmup_updates or self.hard_reset
        ):
            self.warmup_scheduler = optim.lr_scheduler.LambdaLR(
                optimizer, self._warmup_lr
            )
        else:
            self.warmup_scheduler = None

    def _is_lr_warming_up(self):
        """
        Check if we're warming up the learning rate.
        """
        return (
            hasattr(self, 'warmup_scheduler')
            and self.warmup_scheduler is not None
            and self._number_training_updates <= self.warmup_updates
        )

    def _warmup_lr(self, step):
        """
        Return lr multiplier (on initial lr) for warmup scheduler.
        """
        start = self.warmup_rate
        end = 1.0
        progress = min(1.0, step / self.warmup_updates)
        lr_mult = start + (end - start) * progress
        return lr_mult

    def load_state(self, states):
        """
        Load state of scheduler from states.
        """
        if self.scheduler and 'lr_scheduler' in states:
            self.scheduler.load_state_dict(states['lr_scheduler'])
        if states.get('warmup_scheduler') and getattr(self, 'warmup_scheduler', None):
            self.warmup_scheduler.load_state_dict(states['warmup_scheduler'])
        self._number_training_updates = states.get('number_training_updates', 0)
        self.step(self._number_training_updates)

    def get_initial_number_training_updates(self):
        return self._number_training_updates

    def get_state_dict(self):
        """
        Return scheduler state dictionary.
        """
        return self.scheduler.state_dict()

    def get_warmup_state_dict(self):
        """
        Return warmup scheduler state dictionary.
        """
        if self.warmup_scheduler is None:
            return None
        return self.warmup_scheduler.state_dict()

    @classmethod
    def lr_scheduler_factory(cls, opt, optimizer, states, hard_reset=False):
        """
        Create the learning rate scheduler, and assign it to self.scheduler. This
        scheduler will be updated upon a call to receive_metrics. May also create
        self.warmup_scheduler, if appropriate.

        :param opt opt:
            Arguments received by torch_agent
        :param optimizer optimizer:
            Optimizer being used for training. May be wrapped in
            fp16_optimizer_wrapper depending on whether fp16 is used.
        :param state_dict states:
            Possible state_dict provided by model checkpoint, for restoring
            LR state.
        :param bool hard_reset:
            If true, the LR scheduler should ignore the state dictionary.
        :return: LRScheduler object
        """

        patience = opt.get('lr_scheduler_patience', 3)
        decay = opt.get('lr_scheduler_decay', 0.5)
        warmup_updates = opt.get('warmup_updates', -1)
        warmup_rate = opt.get('warmup_rate', 1e-4)
        max_lr_steps = opt.get('max_lr_steps', -1)
        invsqrt_lr_decay_gamma = opt.get('invsqrt_lr_decay_gamma', -1)

        if opt.get('lr_scheduler') == 'none':
            return None
        elif decay == 1.0:
            logger.warning(
                "Your LR decay is set to 1.0. Assuming you meant you wanted "
                "to disable learning rate scheduling. Adjust lr_scheduler_decay "
                "if this is not correct."
            )
            return None
        elif opt.get('lr_scheduler') == 'reduceonplateau':
            scheduler = ReduceOnPlateauLRScheduler(
                optimizer, hard_reset, patience, decay, warmup_updates, warmup_rate
            )
        elif opt.get('lr_scheduler') == 'fixed':
            scheduler = FixedLRScheduler(
                optimizer, hard_reset, patience, decay, warmup_updates, warmup_rate
            )
        elif opt.get('lr_scheduler') == 'invsqrt':
            scheduler = InvSqrtLRScheduler(
                optimizer,
                hard_reset,
                patience,
                decay,
                warmup_updates,
                warmup_rate,
                invsqrt_lr_decay_gamma,
            )
        elif opt.get('lr_scheduler') == 'cosine':
            scheduler = CosineLRScheduler(
                optimizer,
                hard_reset,
                patience,
                decay,
                warmup_updates,
                warmup_rate,
                max_lr_steps,
            )
        elif opt.get('lr_scheduler') == 'linear':
            scheduler = LinearLRScheduler(
                optimizer,
                hard_reset,
                patience,
                decay,
                warmup_updates,
                warmup_rate,
                max_lr_steps,
            )
        else:
            raise ValueError(
                "Don't know what to do with lr_scheduler '{}'".format(
                    opt.get('lr_scheduler')
                )
            )

        # time to load LR state from the checkpoint, if possible.
        if (
            # there is already an old LR scheduler saved on disk
            states
            # and there was a scheduler in the dump
            and 'lr_scheduler_type' in states
            # and the old LR scheduler is different
            and states.get('lr_scheduler_type') != opt['lr_scheduler']
            # and we're not already using a fresh scheduler
            and not hard_reset
        ):
            # the LR scheduler changed, start things fresh
            logger.warning(
                f"LR scheduler ({opt['lr_scheduler']}) is different from saved "
                f"({states.get('lr_scheduler_type')}). Starting fresh!"
            )
            hard_reset = True

        if not hard_reset:
            # do the actual loading (if possible)
            scheduler.load_state(states)

        # setup warmup scheduler after loading saved scheduler
        scheduler._init_warmup_scheduler(optimizer, states)

        return scheduler

    def step(self, num_steps):
        """
        Use the number of train steps to adjust the warmup scheduler or the main
        scheduler, depending on where in training we are.

        Override this method to override the behavior for training schedulers.
        """
        self._number_training_updates = num_steps
        if self._is_lr_warming_up():
            self.warmup_scheduler.step(epoch=num_steps)
        else:
            scheduler_steps = num_steps - self.warmup_updates
            self.train_step(scheduler_steps)

    @abstractmethod
    def train_step(self, scheduler_steps):
        """
        Use the number of train steps to decide when to adjust LR schedule.

        Override this method to override the behavior for training schedulers.
        """
        pass

    @abstractmethod
    def valid_step(self, metric):
        """
        Use the metrics to decide when to adjust LR schedule.

        This uses the loss as the validation metric if present, if not this
        function does nothing. Note that the model must be reporting loss for
        this to work.

        Override this method to override the behavior for validation schedulers.
        """
        pass


class ReduceOnPlateauLRScheduler(LRScheduler):
    """
    Scheduler that decays by a multiplicative rate when valid loss plateaus.
    """

    def __init__(
        self, optimizer, hard_reset, patience, decay, warmup_updates, warmup_rate
    ):
        super().__init__(hard_reset, warmup_updates, warmup_rate)
        self.scheduler = optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, 'min', factor=decay, patience=patience, verbose=True
        )

    def train_step(self, scheduler_steps):
        pass

    def valid_step(self, metric):
        if self._is_lr_warming_up():
            # we're not done warming up, so don't start using validation
            # metrics to adjust schedule
            return
        self.scheduler.step(metric)


class FixedLRScheduler(LRScheduler):
    """
    Scheduler that decays by a fixed multiplicative rate at each valid step.
    """

    def __init__(
        self, optimizer, hard_reset, patience, decay, warmup_updates, warmup_rate
    ):
        super().__init__(hard_reset, warmup_updates, warmup_rate)
        self.scheduler = optim.lr_scheduler.StepLR(optimizer, patience, gamma=decay)

    def train_step(self, scheduler_steps):
        pass

    def valid_step(self, metric):
        if self._is_lr_warming_up():
            # we're not done warming up, so don't start using validation
            # metrics to adjust schedule
            return
        self.scheduler.step()


class InvSqrtLRScheduler(LRScheduler):
    """
    Scheduler that decays at an inverse square root rate.
    """

    def __init__(
        self,
        optimizer,
        hard_reset,
        patience,
        decay,
        warmup_updates,
        warmup_rate,
        invsqrt_lr_decay_gamma,
    ):
        """
        invsqrt_lr_decay_gamma determines the cycle length of the inverse square root
        scheduler.

        When steps taken == invsqrt_lr_decay_gamma, the lr multiplier is 1
        """
        super().__init__(hard_reset, warmup_updates, warmup_rate)
        self.invsqrt_lr_decay_gamma = invsqrt_lr_decay_gamma
        if invsqrt_lr_decay_gamma <= 0:
            logger.warning(
                '--lr-scheduler invsqrt requires a value for '
                '--invsqrt-lr-decay-gamma. Defaulting to set gamma to '
                '--warmup-updates value for backwards compatibility.'
            )
            self.invsqrt_lr_decay_gamma = self.warmup_updates

        self.decay_factor = np.sqrt(max(1, self.invsqrt_lr_decay_gamma))
        self.scheduler = optim.lr_scheduler.LambdaLR(optimizer, self._invsqrt_lr)

    def _invsqrt_lr(self, step):
        return self.decay_factor / np.sqrt(max(1, self.invsqrt_lr_decay_gamma + step))

    def train_step(self, scheduler_steps):
        self.scheduler.step(epoch=scheduler_steps)

    def valid_step(self, metric):
        # this is a training step lr scheduler, nothing to adjust in validation
        pass


class CosineLRScheduler(LRScheduler):
    """
    Scheduler that decays by a cosine function.
    """

    def __init__(
        self,
        optimizer,
        hard_reset,
        patience,
        decay,
        warmup_updates,
        warmup_rate,
        max_lr_steps,
    ):
        """
        max_lr_steps determines the cycle length of the cosine annealing.

        It indicates the number of steps from 1.0 multiplier to 0.0, which corresponds
        to going from cos(0) to cos(pi)
        """
        super().__init__(hard_reset, warmup_updates, warmup_rate)
        if max_lr_steps <= 0:
            raise ValueError('--lr-scheduler cosine requires setting --max-lr-steps')
        self.max_lr_steps = max_lr_steps
        self.scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, max_lr_steps)

    def train_step(self, scheduler_steps):
        self.scheduler.step(epoch=scheduler_steps)

    def valid_step(self, metric):
        pass


class LinearLRScheduler(LRScheduler):
    """
    Scheduler that decays linearly.
    """

    def __init__(
        self,
        optimizer,
        hard_reset,
        patience,
        decay,
        warmup_updates,
        warmup_rate,
        max_lr_steps,
    ):
        """
        max_lr_steps determines the cycle length of the linear annealing.

        It indicates the number of steps from 1.0 multiplier to 0.0
        """
        super().__init__(hard_reset, warmup_updates, warmup_rate)
        if max_lr_steps <= 0:
            raise ValueError('lr_scheduler linear requires setting max_lr_steps')
        self.max_lr_steps = max_lr_steps
        self.scheduler = optim.lr_scheduler.LambdaLR(optimizer, self._linear_lr)

    def _linear_lr(self, step):
        # this multiplicative factor ensures linear decay rate
        # lr_mult = float(self.max_lr_steps - step - 1) / float(self.max_lr_steps - step)
        lr_mult = max(0.0, 1.0 - step / self.max_lr_steps)
        return lr_mult

    def train_step(self, scheduler_steps):
        if scheduler_steps >= self.max_lr_steps:
            raise Exception('End of Linear LR Schedule')
        self.scheduler.step(epoch=scheduler_steps)

    def valid_step(self, metric):
        pass