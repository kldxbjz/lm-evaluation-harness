import abc
import hashlib
import json
import logging
import os
from typing import Callable, Dict, List, Optional, Tuple, Type, TypeVar

import transformers
from sqlitedict import SqliteDict
from tqdm import tqdm

from lm_eval import utils
from lm_eval.api.instance import Instance

eval_logger = logging.getLogger("lm-eval")


T = TypeVar("T", bound="LM")


class LM(abc.ABC):
    def __init__(self) -> None:
        """Defines the interface that should be implemented by all LM subclasses.
        LMs are assumed to take text (strings) as input and yield strings as output
        (inputs/outputs should be tokenization-agnostic.)

        """
        # set rank and world size to a single process, by default.
        self._rank = 0
        self._world_size = 1
        self.cache_hook = CacheHook(None)

    @abc.abstractmethod
    def loglikelihood(self, requests) -> List[Tuple[float, bool]]:
        """Compute log-likelihood of generating a continuation from a context.
        Downstream tasks should attempt to use loglikelihood instead of other
        LM calls whenever possible.

        :param requests: list[Instance]
            A list of Instance objects, with property `args` which returns a tuple (context, continuation).
            `context: str`
                Context string. Implementations of LM must be able to handle an
                empty context string.
            `continuation: str`
                The continuation over which log likelihood will be calculated. If
                there is a word boundary, the space should be in the continuation.
                For example, context="hello" continuation=" world" is correct.

        :return: list[tuple[float, bool]]
            A list of pairs (logprob, isgreedy)
            `logprob: float`
                The log probability of `continuation`.
            `isgreedy`:
                Whether `continuation` would be generated by greedy sampling from `context`.
        """
        pass

    @abc.abstractmethod
    def loglikelihood_rolling(self, requests) -> List[Tuple[float]]:
        """Compute full log-likelihood of a string, with no truncation, for perplexity computation
        - We will use the full max context length of the model.
        - For inputs that exceed the max context length, we divide the tokenized string into chunks of up to
        the max context length.
        - IMPORTANT: Each document's loglikelihood/perplexity is computed *separately*, unlike other implementations
          which may simply concatenate multiple documents together.
        - IMPORTANT: We maximize the amount of context for each prediction. Specifically, for inputs that we break into
          multiple chunks, the last input will still a full-sized context.
          Example:
            Input tokens: [ 0 1 2 3 4 5 6 7 8 9 ]
            Prefix: BOS/EOS
            Max context length: 4
            Resulting input/prediction pairs:

                INPUT:  BOS   0   1   2
                PRED:     0   1   2   3

                INPUT:    3   4   5   6
                PRED:     4   5   6   7

                INPUT:    5   6   7   8
                PRED:             8   9

          Observe that:
            1. Each token is predicted exactly once
            2. For the last pair, we provide the full context, but only score the last two tokens

        :param requests: list[Instance]
            A list of Instance objects with property `args` which returns a tuple (context,).
            string: str
                String for which we are computing overall loglikelihood
        :return: list[tuple[float]]
            A list of tuples (logprob,)
            logprob: float
                The log probability of `context` conditioned on the BOS/EOS token.
                Can also be overridden for custom cases by `prefix_token_id`.
        """
        pass

    # TODO: Add an optional max length
    @abc.abstractmethod
    def generate_until(self, requests) -> List[str]:
        """Generate greedily until a stopping sequence

        :param requests: list[Instance]
            A list of Instance objects with property `args` which returns a tuple (context, until).
            context: str
                Context string
            until: [str]
                The string sequences to generate until. These string sequences
                may each span across multiple tokens, or may be part of one token.
        :return: list[str]
            A list of strings continuation
            continuation: str
                The generated continuation.
        """
        pass

    def apply_chat_template(self, chat_history: List[Dict[str, str]]) -> str:
        """
        Defines how to transform few-shot examples provided as chat history into a format that can be used as input to the LM.

        :param chat_history: list[dict[str, str]]
            A list of dictionaries with keys 'role' and 'content'.
            Values are strings representing the role name and the content of the message, respectively.
        :return: str
            A string representing the chat history in a format that can be used as input to the LM.
        """
        raise NotImplementedError(
            "To use this model with chat templates, please implement the 'apply_chat_template' method for your model type."
        )

    @classmethod
    def create_from_arg_string(
        cls: Type[T], arg_string: str, additional_config: Optional[dict] = None
    ) -> T:
        """
        Creates an instance of the LM class using the given argument string and additional config.

        Parameters:
        - arg_string: A string containing arguments in the format key1=value1,key2=value2.
        - additional_config: Optional dictionary containing additional configuration parameters.

        Returns:
        - Instance of the LM class.
        """
        additional_config = {} if additional_config is None else additional_config
        args = utils.simple_parse_args_string(arg_string)
        args2 = {k: v for k, v in additional_config.items() if v is not None}
        return cls(**args, **args2)

    @classmethod
    def create_from_arg_obj(
        cls: Type[T], arg_dict: dict, additional_config: Optional[dict] = None
    ) -> T:
        """
        Creates an instance of the LM class using the given arg_obj

        Parameters:
        - arg_obj: A dict containing arguments in the format key1=value1,key2=value2.
        - additional_config: Optional dictionary containing additional configuration parameters.

        Returns:
        - Instance of the LM class.
        """

        additional_config = {} if additional_config is None else additional_config
        additional_config = {
            k: v for k, v in additional_config.items() if v is not None
        }

        return cls(**arg_dict, **additional_config)

    @property
    def rank(self):
        # used in the case of parallelism. Hardcoded to
        # ensure no errors arise using API models which do
        # not support multi-device parallelism nor expect it.
        return self._rank

    @property
    def world_size(self):
        # used in the case of parallelism. Hardcoded to
        # ensure no errors arise using API models which do
        # not support multi-device parallelism nor expect it.
        return self._world_size

    @property
    def tokenizer_name(self) -> str:
        """Must be defined for LM subclasses which implement Chat Templating.
        Should return the name of the tokenizer or chat template used.
        Used only to properly fingerprint caches when requests are being cached with `--cache_requests`, otherwise not used.
        """
        raise NotImplementedError(
            "To use this model with chat templates, please implement the 'tokenizer_name' property."
        )

    @property
    def chat_template(self) -> str:
        """Must be defined for LM subclasses that implement Chat Templating.
        Should return the structure of the chat template applied to user/assistant messages.
        This is used only to save in the experiment results for reproducibility.
        """
        raise NotImplementedError(
            "To use this model with chat templates, please implement the 'chat_template' property."
        )

    def set_cache_hook(self, cache_hook) -> None:
        self.cache_hook = cache_hook


# For now, let's read the first line
class PrefixLM(LM):
    def __init__(
        self, base_lm: LM, path="/data/zora_che/robust-unlearning-benchmark/test.csv"
    ):
        super().__init__()
        self.base_lm = base_lm
        self.path = path
        if "txt" in self.path:
            with open(self.path, "r") as file:
                self.prefix = file.readline().strip()
                # print("Initializing-----This is the prefix")
                # print(self.prefix)
                # break
        else:
            self.prefix = self.path
        self.add_prefix = lambda x: f"{self.prefix} {x}"

    def _safe_prefix(self, text: str) -> str:
        llama_tags = [
            "<|begin_of_text|>",
            "<|start_header_id|>",
        ]
        for tag in llama_tags:
            assert (
                tag not in text
            ), f"LLaMA tag '{tag}' found in text to be encrypted. This is not allowed."
        # print("adding prefix")
        print(self.add_prefix(text))
        return self.add_prefix(text)

    def loglikelihood(self, requests) -> List[Tuple[float, bool]]:
        prefixed_requests = []
        for req in requests:
            context, continuation = req.args
            # Add the prefix only if it is not already present in the context
            if not context.startswith(self.prefix):
                context = self._safe_prefix(context)
            prefixed_req = Instance(
                request_type=req.request_type,
                doc=req.doc,
                arguments=(context, continuation),  # continuation remains unprefixed
                idx=req.idx,
                metadata=req.metadata,
            )
            prefixed_requests.append(prefixed_req)

        results = self.base_lm.loglikelihood(prefixed_requests)
        return results
        # prefixed_requests = []
        # for req in requests:
        #     context, continuation = req.args
        #     # Add the prefix only if it is not already present in the continuation
        #     if not continuation.startswith(self.prefix):
        #         continuation = self._safe_prefix(continuation)
        #     prefixed_req = Instance(
        #         request_type=req.request_type,
        #         doc=req.doc,
        #         arguments=(context, continuation),  # context remains unprefixed
        #         idx=req.idx,
        #         metadata=req.metadata
        #     )
        #     prefixed_requests.append(prefixed_req)

        # results = self.base_lm.loglikelihood(prefixed_requests)
        # return results

    def loglikelihood_rolling(self, requests) -> List[Tuple[float]]:
        prefixed_requests = []
        for req in requests:
            (context,) = req.args
            # Add the prefix only if it is not already present in the context
            if not context.startswith(self.prefix):
                context = self._safe_prefix(context)
            prefixed_req = Instance(
                request_type=req.request_type,
                doc=req.doc,
                arguments=(context,),
                idx=req.idx,
                metadata=req.metadata,
            )
            prefixed_requests.append(prefixed_req)

        results = self.base_lm.loglikelihood_rolling(prefixed_requests)
        return results
        # prefixed_requests = []
        # for req in requests:
        #     context, = req.args
        #     # Add the prefix only if it is not already present in the context
        #     if not context.startswith(self.prefix):
        #         context = self._safe_prefix(context)
        #     prefixed_req = Instance(
        #         request_type=req.request_type,
        #         doc=req.doc,
        #         arguments=(context,),
        #         idx=req.idx,
        #         metadata=req.metadata
        #     )
        #     prefixed_requests.append(prefixed_req)

        # results = self.base_lm.loglikelihood_rolling(prefixed_requests)
        # return results

    def generate_until(self, requests) -> List[str]:
        prefixed_requests = []
        for req in requests:
            context, until = req.args
            # Add the prefix only if it is not already present in the context
            if not context.startswith(self.prefix):
                context = self._safe_prefix(context)
                # print("the current context!")
                # print(context)
                # print("_____")
            prefixed_req = Instance(
                request_type=req.request_type,
                doc=req.doc,
                arguments=(context, until),
                idx=req.idx,
                metadata=req.metadata,
            )
            prefixed_requests.append(prefixed_req)

        results = self.base_lm.generate_until(prefixed_requests)
        return results

    # def apply_chat_template(self, chat_history: List[Dict[str, str]]) -> str:
    #     # Encrypt each message in the chat history, except for system messages
    #     encrypted_chat_history = [
    #         {
    #             "role": message["role"],
    #             "content": message["content"] if message["role"] == "system" else self._safe_encrypt(message["content"])
    #         }
    #         for message in chat_history
    #     ]
    #     # Apply the chat template of the base model
    #     encrypted_result = self.base_lm.apply_chat_template(encrypted_chat_history)
    #     # No need to decrypt here as this will be used as input to the model
    #     return encrypted_result

    @property
    def rank(self):
        return self.base_lm.rank

    @property
    def world_size(self):
        return self.base_lm.world_size

    @property
    def tokenizer_name(self) -> str:
        return self.base_lm.tokenizer_name

    @property
    def chat_template(self) -> str:
        return self.base_lm.chat_template

    def set_cache_hook(self, cache_hook) -> None:
        self.base_lm.set_cache_hook(cache_hook)

    def __getattr__(self, attr):
        # Fallback to base_lm for any attributes not explicitly defined
        return getattr(self.base_lm, attr)


class SuffixLM(LM):
    def __init__(
        self, base_lm: LM, path="/data/zora_che/robust-unlearning-benchmark/test.csv"
    ):
        super().__init__()
        self.base_lm = base_lm
        self.path = path
        if "txt" in self.path:
            with open(self.path, "r") as file:
                self.suffix = file.readline().strip()
                # print("Initializing-----This is the prefix")
                # print(self.prefix)
                # break
        elif ".csv" in self.path:
            with open(self.path, "r") as file:
                self.suffix = [line.strip() for line in file]
        # self.add_suffix = lambda x: f"{self.suffix} {x}"

    def _safe_suffix(self, text: str) -> str:
        llama_tags = [
            "<|begin_of_text|>",
            "<|start_header_id|>",
        ]
        for tag in llama_tags:
            assert (
                tag not in text
            ), f"LLaMA tag '{tag}' found in text to be encrypted. This is not allowed."

        modified_text = text.replace("?\nA.", f"{self.suffix[0]}?\nA.")

        print(modified_text)
        return modified_text

        # print("adding suffix")
        # print(self.add_suffix(text))
        # return self.add_suffix(text)

    def loglikelihood(self, requests) -> List[Tuple[float, bool]]:
        suffixed_requests = []
        for req in requests:
            context, continuation = req.args
            # Add the suffix only if it is not already present in the context
            if "?\nA." in context:
                context = self._safe_suffix(context)
            suffixed_req = Instance(
                request_type=req.request_type,
                doc=req.doc,
                arguments=(context, continuation),  # continuation remains unsuffixed
                idx=req.idx,
                metadata=req.metadata,
            )
            suffixed_requests.append(suffixed_req)

        results = self.base_lm.loglikelihood(suffixed_requests)
        return results
        # suffixed_requests = []
        # for req in requests:
        #     context, continuation = req.args
        #     # Add the suffix only if it is not already present in the continuation
        #     if not continuation.startswith(self.suffix):
        #         continuation = self._safe_suffix(continuation)
        #     suffixed_req = Instance(
        #         request_type=req.request_type,
        #         doc=req.doc,
        #         arguments=(context, continuation),  # context remains unsuffixed
        #         idx=req.idx,
        #         metadata=req.metadata
        #     )
        #     suffixed_requests.append(suffixed_req)

        # results = self.base_lm.loglikelihood(suffixed_requests)
        # return results

    def loglikelihood_rolling(self, requests) -> List[Tuple[float]]:
        suffixed_requests = []
        for req in requests:
            (context,) = req.args
            # Add the suffix only if it is not already present in the context
            if "? \nA. " in context:
                context = self._safe_suffix(context)
            suffixed_req = Instance(
                request_type=req.request_type,
                doc=req.doc,
                arguments=(context,),
                idx=req.idx,
                metadata=req.metadata,
            )
            suffixed_requests.append(suffixed_req)

        results = self.base_lm.loglikelihood_rolling(suffixed_requests)
        return results
        # suffixed_requests = []
        # for req in requests:
        #     context, = req.args
        #     # Add the suffix only if it is not already present in the context
        #     if not context.startswith(self.suffix):
        #         context = self._safe_suffix(context)
        #     suffixed_req = Instance(
        #         request_type=req.request_type,
        #         doc=req.doc,
        #         arguments=(context,),
        #         idx=req.idx,
        #         metadata=req.metadata
        #     )
        #     suffixed_requests.append(suffixed_req)

        # results = self.base_lm.loglikelihood_rolling(suffixed_requests)
        # return results

    def generate_until(self, requests) -> List[str]:
        suffixed_requests = []
        for req in requests:
            context, until = req.args
            # Add the suffix only if it is not already present in the context
            if not context.startswith(self.suffix):
                context = self._safe_suffix(context)
                # print("the current context!")
                # print(context)
                # print("_____")
            suffixed_req = Instance(
                request_type=req.request_type,
                doc=req.doc,
                arguments=(context, until),
                idx=req.idx,
                metadata=req.metadata,
            )
            suffixed_requests.append(suffixed_req)

        results = self.base_lm.generate_until(suffixed_requests)
        return results

    # def apply_chat_template(self, chat_history: List[Dict[str, str]]) -> str:
    #     # Encrypt each message in the chat history, except for system messages
    #     encrypted_chat_history = [
    #         {
    #             "role": message["role"],
    #             "content": message["content"] if message["role"] == "system" else self._safe_encrypt(message["content"])
    #         }
    #         for message in chat_history
    #     ]
    #     # Apply the chat template of the base model
    #     encrypted_result = self.base_lm.apply_chat_template(encrypted_chat_history)
    #     # No need to decrypt here as this will be used as input to the model
    #     return encrypted_result

    @property
    def rank(self):
        return self.base_lm.rank

    @property
    def world_size(self):
        return self.base_lm.world_size

    @property
    def tokenizer_name(self) -> str:
        return self.base_lm.tokenizer_name

    @property
    def chat_template(self) -> str:
        return self.base_lm.chat_template

    def set_cache_hook(self, cache_hook) -> None:
        self.base_lm.set_cache_hook(cache_hook)

    def __getattr__(self, attr):
        # Fallback to base_lm for any attributes not explicitly defined
        return getattr(self.base_lm, attr)


class CipherLM(LM):
    def __init__(
        self, base_lm: LM, encrypt: Callable, decrypt: Callable, path="path/to/prefix"
    ):
        super().__init__()
        self.base_lm = base_lm
        self.encrypt = lambda x: f"x"
        self.decrypt = decrypt
        self.path = path

    def _safe_encrypt(self, text: str) -> str:
        llama_tags = [
            "<|begin_of_text|>",
            "<|start_header_id|>",
        ]
        for tag in llama_tags:
            assert (
                tag not in text
            ), f"LLaMA tag '{tag}' found in text to be encrypted. This is not allowed."
        return self.encrypt(text)

    def _safe_decrypt(self, text: str) -> str:
        llama_tags = [
            "<|begin_of_text|>",
            "<|start_header_id|>",
        ]
        for tag in llama_tags:
            assert (
                tag not in text
            ), f"LLaMA tag '{tag}' found in text to be decrypted. This is not allowed."
        return self.decrypt(text)

    def loglikelihood(self, requests) -> List[Tuple[float, bool]]:
        encrypted_requests = []
        for req in requests:
            context, continuation = req.args
            # Only encrypt the continuation (document response)
            encrypted_continuation = self._safe_encrypt(continuation)
            encrypted_req = Instance(
                request_type=req.request_type,
                doc=req.doc,
                arguments=(
                    context,
                    encrypted_continuation,
                ),  # context remains unencrypted
                idx=req.idx,
                metadata=req.metadata,
            )
            encrypted_requests.append(encrypted_req)

        results = self.base_lm.loglikelihood(encrypted_requests)
        return results

    def loglikelihood_rolling(self, requests) -> List[Tuple[float]]:
        encrypted_requests = []
        for req in requests:
            (context,) = req.args
            encrypted_context = self._safe_encrypt(context)
            encrypted_req = Instance(
                request_type=req.request_type,
                doc=req.doc,
                arguments=(encrypted_context,),
                idx=req.idx,
                metadata=req.metadata,
            )
            encrypted_requests.append(encrypted_req)

        results = self.base_lm.loglikelihood_rolling(encrypted_requests)
        return results

    def generate_until(self, requests) -> List[str]:
        results = self.base_lm.generate_until(requests)
        return [self._safe_decrypt(result) for result in results]

    def apply_chat_template(self, chat_history: List[Dict[str, str]]) -> str:
        # Encrypt each message in the chat history, except for system messages
        encrypted_chat_history = [
            {
                "role": message["role"],
                "content": (
                    message["content"]
                    if message["role"] == "system"
                    else self._safe_encrypt(message["content"])
                ),
            }
            for message in chat_history
        ]
        # Apply the chat template of the base model
        encrypted_result = self.base_lm.apply_chat_template(encrypted_chat_history)
        # No need to decrypt here as this will be used as input to the model
        return encrypted_result

    @property
    def rank(self):
        return self.base_lm.rank

    @property
    def world_size(self):
        return self.base_lm.world_size

    @property
    def tokenizer_name(self) -> str:
        return self.base_lm.tokenizer_name

    @property
    def chat_template(self) -> str:
        return self.base_lm.chat_template

    def set_cache_hook(self, cache_hook) -> None:
        self.base_lm.set_cache_hook(cache_hook)

    def __getattr__(self, attr):
        # Fallback to base_lm for any attributes not explicitly defined
        return getattr(self.base_lm, attr)


### SQLite-based caching of LM responses
def hash_args(attr, args):
    dat = json.dumps([attr] + list(args))
    return hashlib.sha256(dat.encode("utf-8")).hexdigest()


class CacheHook:
    def __init__(self, cachinglm) -> None:
        if cachinglm is None:
            self.dbdict = None
            return

        self.dbdict = cachinglm.dbdict

    def add_partial(self, attr, req, res) -> None:
        if self.dbdict is None:
            return
        hsh = hash_args(attr, req)
        self.dbdict[hsh] = res


class CachingLM:
    def __init__(self, lm, cache_db) -> None:
        """LM wrapper that returns cached results if they exist, and uses the underlying LM if not.

        :param lm: LM
            Underlying LM
        :param cache_db: str
            Path to cache db
        """
        self.lm = lm
        self.cache_db = cache_db
        if os.path.dirname(cache_db):
            os.makedirs(os.path.dirname(cache_db), exist_ok=True)
        self.dbdict = SqliteDict(cache_db, autocommit=True)

        # add hook to lm
        lm.set_cache_hook(self.get_cache_hook())

    def __getattr__(self, attr: str):
        lm_attr = getattr(self.lm, attr)
        if attr not in ["loglikelihood", "loglikelihood_rolling", "generate_until"]:
            eval_logger.debug(f"Passing through attribute '{attr}' to underlying LM")
            return lm_attr

        def fn(requests):
            res = []
            remaining_reqs = []
            warned = False
            # figure out which ones are cached and which ones are new
            eval_logger.info(
                f"Loading '{attr}' responses from cache '{self.cache_db}' where possible..."
            )
            for req in tqdm(requests, desc="Checking cached requests"):
                hsh = hash_args(attr, req.args)
                if attr == "generate_until" and req.args[1].get("do_sample", False):
                    # when we are doing non-greedy generation, don't use the cache
                    # (else every "randomly sampled" generation would be identical for repeats > 1).
                    if not warned:
                        eval_logger.warning(
                            f"Arguments to lm.generate_until() '{req.args[1]}' include non-deterministic sampling. Caching will not be performed for such requests."
                        )
                        warned = True
                    res.append(None)
                    remaining_reqs.append(req)
                elif hsh in self.dbdict:
                    ob = self.dbdict[hsh]

                    assert ob is not None

                    res.append(ob)
                else:
                    res.append(None)
                    remaining_reqs.append(req)
            eval_logger.info(
                f"Cached requests: {len(requests) - len(remaining_reqs)}, Requests remaining: {len(remaining_reqs)}"
            )
            # actually run the LM on the requests that do not have cached results
            rem_res = getattr(self.lm, attr)(remaining_reqs)

            # stick the new ones back into the list and also cache any of the new ones
            resptr = 0
            for req, r in zip(remaining_reqs, rem_res):
                while res[resptr] is not None:
                    resptr += 1

                res[resptr] = r

                # caching
                hsh = hash_args(attr, req.args)
                self.dbdict[hsh] = r
            self.dbdict.commit()

            return res

        return fn

    def get_cache_hook(self):
        return CacheHook(self)


class TemplateLM(LM):
    """
    A class acting as intermediary between the LM base class
    and boilerplate often included in other LM subclasses.
    """

    @property
    @abc.abstractmethod
    def eot_token_id(self):
        pass

    @property
    def prefix_token_id(self):
        # it is used as prefix for loglikelihood
        return self.eot_token_id

    @abc.abstractmethod
    def tok_encode(self, string: str, **kwargs):
        pass

    @abc.abstractmethod
    def _loglikelihood_tokens(self, requests, **kwargs):
        pass

    def _encode_pair(self, context, continuation):
        n_spaces = len(context) - len(context.rstrip())
        if n_spaces > 0:
            continuation = context[-n_spaces:] + continuation
            context = context[:-n_spaces]

        model_class = getattr(self, "AUTO_MODEL_CLASS", None)

        if model_class == transformers.AutoModelForSeq2SeqLM:
            context_enc = self.tok_encode(context)
            continuation_enc = self.tok_encode(continuation, add_special_tokens=False)
        else:
            whole_enc = self.tok_encode(context + continuation)
            context_enc = self.tok_encode(context)

            context_enc_len = len(context_enc)
            continuation_enc = whole_enc[context_enc_len:]

        return context_enc, continuation_enc

    def loglikelihood(
        self, requests, disable_tqdm: bool = False
    ) -> List[Tuple[float, bool]]:
        new_reqs = []
        for context, continuation in [req.args for req in requests]:
            if context == "":
                # BOS or EOS as context
                context_enc, continuation_enc = (
                    [self.prefix_token_id],
                    self.tok_encode(continuation),
                )
            else:
                context_enc, continuation_enc = self._encode_pair(context, continuation)

            new_reqs.append(((context, continuation), context_enc, continuation_enc))

        return self._loglikelihood_tokens(new_reqs, disable_tqdm=disable_tqdm)

    @abc.abstractmethod
    def loglikelihood_rolling(
        self, requests, disable_tqdm: bool = False
    ) -> List[Tuple[float, bool]]:
        pass

    @abc.abstractmethod
    def generate_until(self, requests, disable_tqdm: bool = False) -> List[str]:
        pass
