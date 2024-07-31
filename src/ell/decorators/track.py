import logging
from ell.types import SerializedLStr
import ell.util.closure
from ell.configurator import config
from ell.lstr import lstr

import inspect

import cattrs
import numpy as np


import hashlib
import json
import secrets
import time
from datetime import datetime
from functools import wraps
from typing import Callable

logger = logging.getLogger(__name__)
def exclude_var(v):
    # is module or is immutable
    return inspect.ismodule(v)

def track(fn: Callable) -> Callable:
    if hasattr(fn, "__ell_lm_kwargs__"):
        func_to_track = fn
        lm_kwargs = fn.__ell_lm_kwargs__
        lmp = True
    else:
        func_to_track = fn
        lm_kwargs = None
        lmp = False


    # see if it exists
    _name = func_to_track.__qualname__
    _has_serialized_lmp = False

    fn_closure : str 
    if not hasattr(func_to_track, "__ell_hash__") and not config.lazy_versioning:
        fn_closure, _ = ell.util.closure.lexically_closured_source(func_to_track)

    @wraps(fn)
    def wrapper(*fn_args, **fn_kwargs) -> str:
        nonlocal _has_serialized_lmp
        nonlocal fn_closure
        # Compute the invocation id and hash the inputs for serialization.
        invocation_id = "invocation-" + secrets.token_hex(16)

        if not config._store:
            return fn(*fn_args, **fn_kwargs, _invocation_origin=invocation_id)[0]


        # Get the list of consumed lmps and clean the invocation paramns for serialization.
        cleaned_invocation_params, input_hash, consumes = prepare_invocation_params(fn_args, fn_kwargs)

        try_use_cache = hasattr(func_to_track.__wrapper__, "__ell_use_cache")
        if  try_use_cache:
            # Todo: add nice logging if verbose for when using a cahced invocaiton. IN a different color with thar args..
            if not hasattr(func_to_track, "__ell_hash__")  and config.lazy_versioning:
                fn_closure, _ = ell.util.closure.lexically_closured_source(func_to_track)
            cached_invocations = config._store.get_invocations(lmp_filters=dict(lmp_id=func_to_track.__ell_hash__), filters=dict(
                input_hash=input_hash
            ))

            if len(cached_invocations) > 0:
                results =  [SerializedLStr(**d).deserialize() for  d in cached_invocations[0]['results']]
                if len(results) == 1:
                    return results[0]
                else:
                    return results
                # Todo: Unfiy this with the non-cached case. We should go through the same code pathway.
            else:
                logger.info(f"Attempted to use cache on {func_to_track.__qualname__} but it was not cached, or did not exist in the store. Refreshing cache...")
        
        
        _start_time = datetime.now()
        # get the prompt
        (result, invocation_kwargs, metadata) = (
            (fn(*fn_args, **fn_kwargs), None)
            if not lmp
            else fn(*fn_args, _invocation_origin=invocation_id, **fn_kwargs, )
            )
        latency_ms = (datetime.now() - _start_time).total_seconds() * 1000
        usage = metadata.get("usage", {})
        prompt_tokens=usage.get("prompt_tokens", 0)
        completion_tokens=usage.get("completion_tokens", 0)


        if not _has_serialized_lmp:
            if not hasattr(func_to_track, "__ell_hash__")  and config.lazy_versioning:
                fn_closure, _ = ell.util.closure.lexically_closured_source(func_to_track)
            # Compute commit messages if enabled
            commit = None
            lmps = config._store.get_lmps(name=_name)
            version = 0
            already_in_store =any(lmp['lmp_id'] == func_to_track.__ell_hash__ for lmp in lmps)
            if not already_in_store :
                # Do auto commitng and versioning if previous versions exist.
                if len(lmps) > 0 :
                    lmps.sort(key=lambda x: x['created_at'], reverse=True)
                    latest_lmp = lmps[0]


                    version = (latest_lmp['version_number']) + 1
                    if config.autocommit:
                    # Get the latest lmp
                    # sort by created at  
                        from ell.util.differ import write_commit_message_for_diff
                        commit = str(write_commit_message_for_diff(f"{latest_lmp['dependencies']}\n\n{latest_lmp['source']}", f"{fn_closure[1]}\n\n{fn_closure[0]}")[0])

                config._store.write_lmp(
                    lmp_id=func_to_track.__ell_hash__,
                    name=_name,
                    created_at=datetime.now(),
                    source=fn_closure[0],
                    dependencies=fn_closure[1],
                    commit_message=(commit),
                    global_vars={k: v for k, v in func_to_track.__ell_closure__[2].items() if ell.util.closure.is_immutable_variable(v)},
                    free_vars={k: v for k, v in func_to_track.__ell_closure__[3].items() if ell.util.closure.is_immutable_variable(v)},
                    is_lmp=lmp,
                    lm_kwargs=(
                        (lm_kwargs)
                        if lm_kwargs
                    else None
                    ),
                    version_number=version,
                    uses=func_to_track.__ell_uses__,
                )
                _has_serialized_lmp = True


        config._store.write_invocation(id=invocation_id,
            lmp_id=func_to_track.__ell_hash__,  created_at=datetime.now(),
            global_vars={k: v for k, v in func_to_track.__ell_closure__[2].items() if ell.util.closure.is_immutable_variable(v)},
            free_vars={k: v for k, v in func_to_track.__ell_closure__[3].items() if ell.util.closure.is_immutable_variable(v)},
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            input_hash=input_hash,
            invocation_kwargs=invocation_kwargs,
            **cleaned_invocation_params, consumes=consumes, result=result)

        return result

    fn.__wrapper__  = wrapper
    wrapper.__ell_lm_kwargs__ = lm_kwargs
    wrapper.__ell_func__ = func_to_track
    wrapper.__ell_track = True

    return wrapper



def prepare_invocation_params(fn_args, fn_kwargs):
    invocation_params = dict(
        args=(fn_args),
        kwargs=(fn_kwargs),
    )

    invocation_converter = cattrs.Converter()
    consumes = set()

    def process_lstr(obj):
        consumes.update(obj._origin_trace)
        return invocation_converter.unstructure(dict(content=str(obj), **obj.__dict__, __lstr=True))

    invocation_converter.register_unstructure_hook(
        np.ndarray,
        lambda arr: arr.tolist()
    )
    invocation_converter.register_unstructure_hook(
        lstr,
        process_lstr
    )
    invocation_converter.register_unstructure_hook(
        set,
        lambda s: list(sorted(s))
    )
    invocation_converter.register_unstructure_hook(
        frozenset,
        lambda s: list(sorted(s))
    )

    cleaned_invocation_params = invocation_converter.unstructure(invocation_params)
    input_hash = hashlib.sha256(json.dumps(cleaned_invocation_params, sort_keys=True).encode('utf-8')).hexdigest()
    return cleaned_invocation_params, input_hash, consumes
