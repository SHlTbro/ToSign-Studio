"""Explicit R9 numerical environment identity, separate from base asset identity."""
import hashlib,json,platform,importlib.metadata,os

def observed_cudnn_version(torch_module,device):
 # CPU inference with a CUDA wheel never executes cuDNN or queries GPU hardware.
 # Torch 2.10's version() also checks min(device capabilities); an empty visible
 # device set can fail there. Read the same actual library version binding and
 # preserve its compile/runtime compatibility check for this CPU-only case.
 if str(device)=='cpu' and torch_module.version.cuda is not None:
  library=getattr(torch_module._C,'_cudnn',None)
  if library is None:return None
  runtime=library.getRuntimeVersion();compiled=library.getCompileVersion()
  if runtime[0]!=compiled[0] or (runtime[0]<7 and runtime[1]!=compiled[1]) or (runtime[0]>=7 and runtime[1]<compiled[1]):
   raise RuntimeError('E_CUDNN_LIBRARY_VERSION: actual runtime and compiled cuDNN differ')
  return library.getVersionInt()
 return torch_module.backends.cudnn.version()

def execution_environment(device):
 import torch
 import pygame.freetype
 from .deterministic_ops import policy_identity
 packages={name:importlib.metadata.version(name) for name in ['torch','torchvision','diffusers','transformers','accelerate','numpy','pillow','pygame','safetensors','fonttools','huggingface-hub']}
 body={'schema':'r9-numerical-environment/2','python':platform.python_version(),'platform':platform.platform(),'packages':packages,'device':str(device),'cuda_runtime':torch.version.cuda,'cudnn_version':observed_cudnn_version(torch,device),'gpu_name':torch.cuda.get_device_name(0) if str(device).startswith('cuda') else None,'freetype_version':list(pygame.freetype.get_version()),'torch_deterministic_algorithms':torch.are_deterministic_algorithms_enabled(),'matmul_tf32':torch.backends.cuda.matmul.allow_tf32,'cudnn_tf32':torch.backends.cudnn.allow_tf32,'dtype':'float32','amp':False,'default_dtype':str(torch.get_default_dtype()),'cublas_workspace_config':os.environ.get('CUBLAS_WORKSPACE_CONFIG'),'cudnn_benchmark':torch.backends.cudnn.benchmark,'cudnn_deterministic':torch.backends.cudnn.deterministic,'torch_cpu_threads':torch.get_num_threads(),'torch_interop_threads':torch.get_num_interop_threads(),'operator_execution_policy':policy_identity(),'mixed_cuda_cpu_deform':str(device).startswith('cuda')}
 return {**body,'identity_sha256':hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
