# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

from collections import OrderedDict, deque
import copy
import itertools
from itertools import chain
import logging
from math import inf
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional, Type, Union

import torch
import torch.distributed as dist
from torch.nn import Parameter
from torch.optim import SGD, Optimizer

from .utils import Workhandle, recursive_copy_to_device

__all__ = ["OSS"]

if TYPE_CHECKING:  # pragma: no cover
    from torch.optim.optimizer import _params_t
else:
    _params_t = Any

try:
    from torch.distributed import broadcast_object_list  # noqa

    _torch_broadcast_object = True
except ImportError:
    from .utils import broadcast_object

    _torch_broadcast_object = False


class OSS(Optimizer):
    """Wraps an arbitrary :class:`optim.Optimizer <torch.optim.Optimizer>`
    optimizer and shards its state as described by ZeRO_.
    ::

        opt = OSS(params, optim=torch.optim.Adam, lr=0.01)

    .. _ZeRO: https://arxiv.org/abs/1910.02054

    We use a greedy algorithm to pack a number of parameters
    at each rank. Each parameter belongs to a single rank and
    is not divided among rank.

    After each rank completed their parameter update, they broadcast
    the new version of the parameters to all other ranks to synchronize
    the parameters for next round forward/backward computation.

    Args:
        params (list of tensors):
            parameters to be optimized
    Keyword Args:
        optim (torch.nn.Optimizer):
            optimizer to shard (default: SGD)
        group (group):
            torch.distributed group (default: group.WORLD)
        broadcast_buffer_size (int):
            the max size of the buffer used to batch the small parameter tensors, in number of elements (default 16M).
            this will not impact the long term memory consumption, but the peak memory can be impacted by the moment
            when the buffers are allocated and the bucketed params have not yet been relocated to them.
    """

    #: The optimizer used for a given shard
    optim: Optimizer

    in_super_constructor: bool

    def __init__(
        self,
        params: _params_t,
        optim: Type[Optimizer] = SGD,
        group: Optional[Any] = None,
        broadcast_buffer_size: int = 2 ** 24,
        **default: Any,
    ):

        # Hold all the model params in the root .param_groups
        self.in_super_constructor = True
        super().__init__(params, default)
        self.in_super_constructor = False

        # Partition information. lazy evaluation, computed when requested
        self._per_device_params: Dict[torch.device, List[List[Parameter]]] = OrderedDict()  # device, rank, params
        self._param_rank: Dict[torch.Tensor, int] = {}
        self._partition_parameters: List[List[dict]] = []

        # Build the wrapped optimizer, responsible for a shard of the params
        self.group = group if group is not None else dist.group.WORLD
        self.world_size = dist.get_world_size(self.group)
        self.rank = dist.get_rank(self.group)
        self.global_rank = self.get_global_rank(self.group, self.rank)
        self.optim = optim(self.partition_parameters()[self.rank], **default)

        # - Sync local and global param_groups keys
        for global_group, local_group in zip(self.param_groups, self.optim.param_groups):
            for key, value in local_group.items():
                if key != "params":
                    global_group[key] = value

        #  Optional consolidated optimizer state
        self._all_states: List[Dict[str, Any]] = []

        # Current default device is set by the parameters allocated to this rank
        self._device = list(self.per_device_params.keys())[0]
        self.buckets: Dict[torch.device, List[torch.Tensor]] = {}

        # Get the correct size for the buckets, cannot be bigger than the model
        model_size = sum([p.numel() for p in self.param_to_rank.keys()])
        self.bucket_size = min(broadcast_buffer_size, model_size)
        logging.info(
            "Bucket size: {:.2f}M parameters, model size {:.2f}M parameters".format(
                self.bucket_size / 2 ** 20, model_size / 2 ** 20
            )
        )

        # Allocate one buffer per rank and per device to group the small parameters
        for device, per_device in self.per_device_params.items():
            self.buckets[device] = [
                torch.zeros(self.bucket_size, dtype=per_device[0][0].dtype, device=device)
                for _ in range(len(per_device))
            ]
        self.should_bucket_param: List[bool] = []
        self.work_handles: Deque[Workhandle] = deque()
        self._max_work_handles = -1
        self._setup_bucket_strategy()

    # Partition helpers
    def partition_parameters(self) -> List[List[dict]]:
        """Partitions parameters across distributed data parallel ranks.

        Returns a list of param_groups (which is a list of dict) where each
        element of the list contains the param_groups for a rank. Element 0
        corresponds to rank 0, etc. We need all the ranks for the broadcast
        inside step().
        """
        if len(self._partition_parameters) == 0:
            self._partition_parameters = [list() for _ in range(self.world_size)]
            sizes = [0] * self.world_size
            for param_group in self.param_groups:
                param_lists: List[List] = [list() for _ in range(self.world_size)]
                for param in param_group["params"]:
                    # Add this param to rank with smallest size.
                    rank = sizes.index(min(sizes))
                    param_lists[rank].append(param)

                    # We're partitioning the optimizer state,
                    # so trainable parameters are the ones which really count
                    if param.requires_grad:
                        sizes[rank] += param.numel()
                    else:
                        # Spread frozen params on a per-tensor basis
                        # Mostly useful for balance partitions for fine tuning for instance
                        # Not required strictly speaking
                        sizes[rank] += 1

                for rank, params in enumerate(param_lists):
                    param_group_rank = copy.copy(param_group)
                    param_group_rank["params"] = params
                    self._partition_parameters[rank].append(param_group_rank)

        return self._partition_parameters

    @property
    def per_device_params(self) -> Dict[torch.device, List[List[Parameter]]]:
        """Sorted list of all the params, first per device then per rank.

        Within a list params are sorted per number of elements to allow for an easy bucketing.
        """
        if len(self._per_device_params) == 0:
            # Go through all params, log them per device
            # The ordering is important here, needs to be the same on all ranks
            # So that ulterior broadcast calls are matching
            for param_group in self.param_groups:
                for param in param_group["params"]:
                    device = param.device
                    if self._per_device_params.get(device) is None:
                        self._per_device_params[device] = [[] for _ in range(self.world_size)]
                    self._per_device_params[device][self.param_to_rank[param]] += [param]

            # Sort param_lists by size
            for device in self._per_device_params.keys():
                for rank_params in self._per_device_params[device]:
                    rank_params.sort(key=lambda x: x.numel())

        return self._per_device_params

    @property
    def param_to_rank(self) -> Dict[torch.Tensor, int]:
        """param to data parallel rank"""
        if len(self._param_rank) == 0:
            for rank, param_groups in enumerate(self.partition_parameters()):
                for param_group in param_groups:
                    for param in param_group["params"]:
                        self._param_rank[param] = rank

            logging.debug("ZeRO: Parameters dispatched to ranks %s " % list(self._param_rank.values()))

        return self._param_rank

    # NOTE(msb) We add a kwargs in order to support Optimizer sub-classes that support extra kwargs.
    # For example, the apex library contains fused optimizers with a step that supports extra kwargs.
    def step(self, closure: Optional[Callable[[], float]] = None, **kwargs: Any) -> Optional[float]:
        """Performs a single optimization step (parameter update).

        Arguments:
            closure (callable): A closure that reevaluates the model and
                returns the loss. Optional for most optimizers.

        .. note: Any extra parameter is passed to the base optimizer as-is"""

        # Sync oss param_groups attributes in case they've been updated by a scheduler.
        self._sync_param_groups()

        # Run the optimizer step on this shard only:
        if closure is not None:
            loss = self.optim.step(closure=closure, **kwargs)  # type: ignore
        else:
            loss = self.optim.step(**kwargs)

        # Sync all the updated shards in between the ranks
        self._broadcast_params()

        # Sync hypothethical new results from the wrapped optimizer to the exposed param_groups
        self._sync_param_groups(local_to_global=True)

        return loss

    def clip_grad_norm(
        self,
        max_norm: Union[float, int],
        norm_type: Union[float, int] = 2.0,
        filter_params_fn: Callable[[Any], Any] = None,
    ) -> torch.Tensor:
        """
        Clip all gradients at this point in time. The norm is computed over all gradients together, as if they were
        concatenated into a single vector. Gradients are modified in-place.

        Arguments:
            max_norm (float or int): max norm of the gradients
            norm_type (float or int): type of the used p-norm. Can be ``'inf'`` for infinity norm.

        Returns:
            Total norm of the parameters (viewed as a single vector).

        .. note: This is analogous to `torch.nn.utils.clip_grad_norm_` but handles the partitioning and multiple devices per rank
            under the hood. The default torch util is not applicable here, because each rank only has a partial view of all the grads
            in the model, so calling it in the OSS context would lead to different scaling being applied per subset of model parameters

        .. warning: This needs to be called on all ranks, since synchronization primitives will be used

        """

        # Compute the max norm for this shards's worth of gradients
        max_norm = float(max_norm)
        norm_type = float(norm_type)

        # Filter out the grad-less params, concatenate params from all devices
        local_params = itertools.chain(
            *[
                list(filter(lambda x: x.grad is not None, device_params[self.rank]))
                for device_params in self.per_device_params.values()
            ]
        )

        # Option to filter parameters from the grad_norm calculation. This is useful for model parallelism.
        # To avoid double counting, only consider parameters on rank zero + anything marked 'model_parallel'
        # 'model_parallel' flag is set in Megatron-LM:
        # https://github.com/NVIDIA/Megatron-LM/blob/19301985dd31c8b612095cbad15bd903e8ddd497/megatron/mpu/layers.py#L54
        if filter_params_fn is not None:
            local_params = filter_params_fn(local_params)

        # Compute the norm on this grad set,
        # then sync all the norms from all ranks
        if norm_type == inf:
            total_norm = max(p.grad.detach().abs().max().to(self._device) for p in local_params)  # type: ignore
            # all reduce over data parallel and model parallel workers
            dist.all_reduce(total_norm, op=torch.distributed.ReduceOp.MAX, group=dist.group.WORLD)
        else:
            local_norm = torch.norm(
                input=torch.stack([torch.norm(input=p.grad.detach(), p=norm_type, dtype=torch.float32).to(self._device) for p in local_params]),  # type: ignore
                p=norm_type,
            )

            # local norm result can be accumulated with the remote ones if put to the right power
            # n_i = sum_rank(a^p)^1/p
            # -> n_total = all_reduce(n_i^p)^(1/p) = sum_i(n_i^p)^1/p = sum_i(sum_rank(a^p))^1/p
            # all reduce over data parallel and model parallel workers
            total_norm = local_norm ** norm_type
            dist.all_reduce(total_norm)
            total_norm = total_norm ** (1.0 / norm_type)

        clip_coef = torch.tensor(max_norm, dtype=total_norm.dtype, device=total_norm.device) / (total_norm + 1e-6)
        if clip_coef < 1:
            for device, device_params in self.per_device_params.items():
                for p in filter(lambda x: x.grad is not None, device_params[self.rank]):
                    p.grad.detach().mul_(clip_coef.to(device))  # type: ignore

        return total_norm

    # State dict interfaces
    def local_state_dict(self) -> dict:
        """Gets this rank's state_dict.

        Returns:
            The state of the optimizer as a :class:`dict`.
            It contains two entries:

            * state - a dict holding current optimization state. Its content
                differs between optimizer classes.
            * param_groups - a dict containing all parameter groups
        """
        return self.optim.state_dict()

    def consolidate_state_dict(self, recipient_rank: int = 0) -> None:
        """Update the consolidated state_dict list, one per rank.

        .. warning: This needs to be called on all replicas"""

        # Sync lr and other attributes in case its been updated
        self._sync_param_groups()

        if self.rank == recipient_rank:
            # Pull the sharded state from all the other replicas
            # Store all the states in order, rank by rank
            logging.debug("Pulling the sharded optimizer state from all replicas")
            self._all_states = self._collect_sharded_states()
        else:
            # Acknowledge broadcasts, and send this rank's shard when needed
            self._broadcast_state_dict()

    def _broadcast_state_dict(self) -> None:
        """Broadcast this rank's state shard, discard others"""

        # Default to CPU space to gain some memory headroom
        local_cpu_state = recursive_copy_to_device(
            self.local_state_dict(), non_blocking=True, device=torch.device("cpu")
        )

        for rank in range(self.world_size):
            if rank == self.rank:
                # Send the state to the reference replica
                logging.debug(
                    "Sending the sharded optimizer state to the reference replica from rank %s", rank,
                )
                if _torch_broadcast_object:
                    # torch native object broadcast
                    dist.broadcast_object_list([local_cpu_state], src=self.global_rank, group=self.group)
                else:
                    # legacy compatibility for old torch versions
                    broadcast_object(
                        self.local_state_dict(), src_rank=self.global_rank, group=self.group, dist_device=self._device
                    )
            else:
                global_rank = self.get_global_rank(self.group, rank)

                # Discard this tensor/rank, broadcast necessary for syncing and because NCCL does not support gather
                if _torch_broadcast_object:
                    dist.broadcast_object_list([0], src=global_rank, group=self.group)
                else:
                    broadcast_object(
                        torch.tensor([0], dtype=torch.uint8, device=self._device),
                        src_rank=global_rank,
                        group=self.group,
                        dist_device=self._device,
                    )

    def _collect_sharded_states(self) -> List[Dict[str, Any]]:
        """Collect all the state shards, in CPU memory."""
        all_states = []

        for rank in range(self.world_size):
            if rank == self.rank:
                logging.debug("Saving self state")
                all_states.append(
                    recursive_copy_to_device(self.local_state_dict(), non_blocking=True, device=torch.device("cpu"))
                )

                # Sync with other replicas
                if _torch_broadcast_object:
                    # torch native object broadcast
                    dist.broadcast_object_list([0], src=self.global_rank, group=self.group)
                else:
                    # legacy compatibility for old torch versions
                    broadcast_object(
                        torch.tensor([0], dtype=torch.uint8, device=self._device),
                        src_rank=self.global_rank,
                        group=self.group,
                        dist_device=self._device,
                    )
            else:
                # Fetch the optim state from the other replicas
                global_rank = self.get_global_rank(self.group, rank)

                if _torch_broadcast_object:
                    replica_state_l = [0]
                    dist.broadcast_object_list(replica_state_l, src=global_rank, group=self.group)
                    replica_state = replica_state_l[0]
                else:
                    replica_state = broadcast_object(
                        torch.tensor([0], dtype=torch.uint8, device=self._device),
                        src_rank=global_rank,
                        group=self.group,
                        dist_device=self._device,
                    )

                all_states.append(
                    recursive_copy_to_device(replica_state, non_blocking=True, device=torch.device("cpu"))
                )

                logging.debug("State from rank %s received", rank)

        return all_states

    def state_dict(self) -> Dict[str, Any]:
        """Return the last known global optimizer state, which consist of a list of the shards.

        .. warning:
            If the state has not been consolidated, this returns a shard's worth, not the global state.

        .. warning:
            Returning the global state is limited to the replica which was responsible for the consolidation.
            The state may also not be up to date, depending on when `consolidate_state_dict` was last called.
        """

        if len(self._all_states) == 0:
            logging.warning("Optimizer state has not been consolidated. Returning the local state")
            logging.warning("Please call `consolidate_state_dict()` beforehand if you meant to save the global state")
            return self.local_state_dict()

        # Flatten the param_groups, flatten the states
        flat_param_groups: List[Dict[Any, Any]] = []
        flat_state = {}
        i_tensor = 0

        for i, s in enumerate(self._all_states):
            # Each rank owns as many param_groups as the model initially had.
            # We flatten them here, so that upon loading the partitioning will be done from scratch
            if len(flat_param_groups) == 0:
                flat_param_groups = s["param_groups"]
            else:
                for fpg, pg in zip(flat_param_groups, s["param_groups"]):
                    fpg["params"].extend(pg["params"])

            # The original states are indexed with respect to the local tensors
            # we need to take a global index into account
            for state in s["state"].values():
                flat_state[i_tensor] = state
                i_tensor += 1

        return {
            "state": flat_state,
            "param_groups": flat_param_groups,
        }

    @staticmethod
    def rank_local_state_dict(rank: int, state_dict: dict) -> dict:
        """Returns the local_state_dict for a given rank.

        Arguments:
            rank (int): rank to get local_state_dict for
            state_dict (dict): global state_dict
        """
        param_groups = state_dict["param_groups"][state_dict["partition"][rank][0] : state_dict["partition"][rank][1]]
        return {"state": state_dict["state"][rank], "param_groups": param_groups}

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        """Restore the global parameter groups as well as the shard.

        Arguments:
            state_dict (dict): optimizer state. Should be an object returned
                from a call to :meth:`state_dict`
        """

        # Workaround PyTorch bug that casts state (https://github.com/pytorch/pytorch/issues/43706)
        # Copied from https://github.com/pytorch/fairseq/blob/v0.9.0/fairseq/optim/fp16_optimizer.py#L251-L268
        groups = self.optim.param_groups
        saved_groups = state_dict["param_groups"]
        id_map = {
            old_id: p
            for old_id, p in zip(chain(*(g["params"] for g in saved_groups)), chain(*(g["params"] for g in groups)))
        }
        for k, v in state_dict["state"].items():
            if k in id_map:
                param = id_map[k]
                self.optim.state[param] = recursive_copy_to_device(v, non_blocking=True, device=param.device)

        # Update the param_group keys
        for fpg, pg in zip(saved_groups, self.param_groups):
            for key in fpg.keys():
                if key != "params":
                    pg[key] = fpg[key]

        # Sync with the optimizer param groups
        self._sync_param_groups(local_to_global=False)

        # Force a re-partitioning, in case the model changed with the new state
        self._partition_parameters.clear()
        self._per_device_params.clear()
        self._param_rank.clear()

        # Update the bucketing strategy accordingly
        self._setup_bucket_strategy()

    def add_param_group(self, param_group: dict) -> None:
        """Add a param group to the :class:`Optimizer` s `param_groups`.

        This can be useful when fine tuning a pre-trained network as frozen layers can be made
        trainable and added to the :class:`Optimizer` as training progresses.

        Arguments:
            param_group (dict): Specifies what Tensors should be optimized along with group
            specific optimization options

        .. warning: This handles updating the shards on all partitions, but needs to be called on all ranks.
        """

        super().add_param_group(param_group)
        if not self.in_super_constructor:
            # Force a re-partitioning
            self._partition_parameters.clear()
            self._per_device_params.clear()
            self._param_rank.clear()

            param_groups = self.partition_parameters()[self.rank]
            if len(param_groups) == len(self.optim.param_groups) + 1:
                self.optim.add_param_group(param_groups[-1])

            # Update the bucketing strategy accordingly
            self._setup_bucket_strategy()

    @staticmethod
    def get_global_rank(group: Any, rank: int) -> int:
        if group is dist.group.WORLD:
            return rank
        else:
            global_rank = dist.distributed_c10d._get_global_rank(group, rank)
        return global_rank

    def _sync_param_groups(self, local_to_global: bool = False) -> None:
        """Sync learning rate and other optimizer attributes (needed to support schedulers).
        If the global param groups have been altered, and we want to make sure that the
        wrapped optimizer uses the up to date version.
        Conversely if the wrapped optimizer has new keys, we expose them through the global param groups"""

        for global_group, local_group in zip(self.param_groups, self.optim.param_groups):
            # Sync everything but the parameters
            for k in filter(lambda x: x != "params", local_group.keys()):
                if local_to_global:
                    global_group[k] = local_group[k]
                elif k in global_group.keys():
                    local_group[k] = global_group[k]

    def _broadcast_params(self) -> None:
        """Helper function to broadcast all the parameters from a given device"""

        i_param = 0

        for (device, device_params,) in self.per_device_params.items():  # all the params on this device (inc all ranks)
            buckets = self.buckets[device]
            # Bucket and issue all the async calls
            for (src_rank, params), bucket in zip(enumerate(device_params), buckets):
                global_src_rank = self.get_global_rank(self.group, src_rank)

                # Direct broadcasts only
                for param in params:
                    if not self.should_bucket_param[i_param]:
                        self.work_handles.append(
                            Workhandle(
                                handle=dist.broadcast(
                                    tensor=param.data, src=global_src_rank, group=self.group, async_op=True
                                ),
                                callback=None,
                            )
                        )
                    i_param += 1

                # Bucket broadcasts
                self.work_handles.append(
                    Workhandle(
                        handle=dist.broadcast(tensor=bucket, src=global_src_rank, group=self.group, async_op=True),
                        callback=None,
                    )
                )

        self._consume_work_handles()

    def _consume_work_handles(self) -> None:
        """Consume all the futures which are tied to this optimizer's buckets.
        We start from the first/older ones, since they are the most likely to be ready and non-blocking
        """

        while len(self.work_handles) > 0:
            work_handle = self.work_handles.popleft()
            work_handle.handle.wait()
            if work_handle.callback is not None:
                work_handle.callback()

    def _try_consume_work_handle(self) -> None:
        """Try to consume the oldest future. This is non blocking, if not ready we'll pass"""
        while len(self.work_handles) > 0 and self.work_handles[0].handle.is_completed():
            work_handle = self.work_handles.popleft()
            if work_handle.callback is not None:
                work_handle.callback()

    def _setup_bucket_strategy(self) -> None:
        """Tag parameters to either bucket them or broadcast/reduce them directly. The parameters are ordered
        (smallest first), the bucket will hold the smallest elements, the remaining ones will be directly sent
        over the wire.

        Generating the partition once and for all allows us to save some time at runtime, and to know when all the
        network requests have been issued.
        """

        # Determine the max work handles in flight:
        # - count all the buckets on the fly
        self._max_work_handles = 0

        for device, per_rank_params in self.per_device_params.items():
            for dst_rank, params in enumerate(per_rank_params):
                offset = 0

                for param in params:
                    # Criteria to decide whether this parameter is to be bucketed or not:
                    # - enough room in the bucket
                    if param.requires_grad and (offset + param.numel()) < self.bucket_size:
                        self.should_bucket_param.append(True)

                        if offset == 0:
                            # count this bucket, only once
                            self._max_work_handles += 1

                        # This parameter becomes a view of the bucket
                        offset_next = offset + param.numel()

                        self.buckets[device][dst_rank][offset:offset_next].copy_(param.data.flatten())
                        param.data = self.buckets[device][dst_rank][offset:offset_next].view_as(param.data)

                        offset = offset_next
                    else:
                        self.should_bucket_param.append(False)

                # Resize the bucket to remove lost space in the end
                self.buckets[device][dst_rank].resize_(offset)

        # Make sure that the memory previously taken by the bucketed parameters is released
        if self._device.type == "cuda":
            torch.cuda.empty_cache()

        # Determine the max work handles in flight:
        # - all the direct reduce/broadcast
        self._max_work_handles += sum(not value for value in self.should_bucket_param)
