import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

import zmq
from msgspec import msgpack

from vllm.config import (CacheConfig, DecodingConfig, DeviceConfig, LoadConfig,
                         LoRAConfig, ModelConfig, ObservabilityConfig,
                         ParallelConfig, PromptAdapterConfig, SchedulerConfig,
                         SpeculativeConfig)
from vllm.engine.arg_utils import EngineArgs
from vllm.engine.metrics_types import StatLoggerBase
from vllm.inputs import (INPUT_REGISTRY, DecoderOnlyInputs,
                         EncoderDecoderLLMInputs, InputRegistry, PromptType)
from vllm.inputs.preprocess import InputPreprocessor
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.pooling_params import PoolingParams
from vllm.prompt_adapter.request import PromptAdapterRequest
from vllm.sampling_params import SamplingParams
from vllm.transformers_utils.config import try_get_generation_config
from vllm.transformers_utils.tokenizer_group import (
    BaseTokenizerGroup, init_tokenizer_from_configs)
from vllm.usage.usage_lib import UsageContext
from vllm.utils import get_open_port
from vllm.v1.core.scheduler import Scheduler
from vllm.v1.executor.gpu_executor import GPUExecutor
from vllm.v1.request import Request
from vllm.v1.tokenizer.detokenizer import (Detokenizer, DetokenizerInputs,
                                           DetokenizerOutputs)
from vllm.version import __version__ as VLLM_VERSION

logger = init_logger(__name__)


class LLMEngine:

    def __init__(
        self,
        model_config: ModelConfig,
        cache_config: CacheConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
        device_config: DeviceConfig,
        load_config: LoadConfig,
        lora_config: Optional[LoRAConfig],
        speculative_config: Optional[SpeculativeConfig],
        decoding_config: Optional[DecodingConfig],
        observability_config: Optional[ObservabilityConfig],
        prompt_adapter_config: Optional[PromptAdapterConfig],
        log_stats: bool,
        input_registry: InputRegistry = INPUT_REGISTRY,
    ) -> None:
        # Override the configs for V1.
        scheduler_config.max_num_seqs = max(scheduler_config.max_num_seqs, 1024)
        scheduler_config.max_num_batched_tokens = max(
            scheduler_config.max_num_batched_tokens, 4096)

        logger.info(
            "Initializing an LLM engine (v%s) with config: "
            "model=%r, speculative_config=%r, tokenizer=%r, "
            "skip_tokenizer_init=%s, tokenizer_mode=%s, revision=%s, "
            "override_neuron_config=%s, "
            "rope_scaling=%r, rope_theta=%r, tokenizer_revision=%s, "
            "trust_remote_code=%s, dtype=%s, max_seq_len=%d, "
            "download_dir=%r, load_format=%s, tensor_parallel_size=%d, "
            "pipeline_parallel_size=%d, "
            "disable_custom_all_reduce=%s, quantization=%s, "
            "enforce_eager=%s, kv_cache_dtype=%s, "
            "quantization_param_path=%s, device_config=%s, "
            "decoding_config=%r, observability_config=%r, "
            "seed=%d, served_model_name=%s, "
            "num_scheduler_steps=%d, enable_prefix_caching=%s, "
            "use_async_output_proc=%s, mm_processor_kwargs=%s)",
            VLLM_VERSION,
            model_config.model,
            speculative_config,
            model_config.tokenizer,
            model_config.skip_tokenizer_init,
            model_config.tokenizer_mode,
            model_config.revision,
            model_config.override_neuron_config,
            model_config.rope_scaling,
            model_config.rope_theta,
            model_config.tokenizer_revision,
            model_config.trust_remote_code,
            model_config.dtype,
            model_config.max_model_len,
            load_config.download_dir,
            load_config.load_format,
            parallel_config.tensor_parallel_size,
            parallel_config.pipeline_parallel_size,
            parallel_config.disable_custom_all_reduce,
            model_config.quantization,
            model_config.enforce_eager,
            cache_config.cache_dtype,
            model_config.quantization_param_path,
            device_config.device,
            decoding_config,
            observability_config,
            model_config.seed,
            model_config.served_model_name,
            scheduler_config.num_scheduler_steps,
            cache_config.enable_prefix_caching,
            model_config.use_async_output_proc,
            model_config.mm_processor_kwargs,
        )

        self.model_config = model_config
        self.cache_config = cache_config
        self.lora_config = lora_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config
        self.device_config = device_config
        self.speculative_config = speculative_config
        self.load_config = load_config
        self.decoding_config = decoding_config or DecodingConfig()
        self.prompt_adapter_config = prompt_adapter_config
        self.observability_config = observability_config or ObservabilityConfig(
        )
        self.log_stats = log_stats

        assert not self.model_config.skip_tokenizer_init
        self.tokenizer = self._init_tokenizer()
        if self.tokenizer:
            # Ping the tokenizer to ensure liveness if it runs in a
            # different process.
            self.tokenizer.ping()

        # Detokenizer
        # FIXME(woosuk): Currently, the detokenizer is just a hacky prototype.
        # For example, it does not terminate properly. We need to improve this.
        self.port1 = get_open_port()
        self.port2 = get_open_port()
        self.detokenizer = Detokenizer(self.model_config.tokenizer, self.port1,
                                       self.port2)
        self.detokenizer.start()
        self.context = zmq.Context()
        self.push_socket = self.context.socket(zmq.PUSH)
        self.push_socket.connect(f"tcp://localhost:{self.port1}")
        self.pull_socket = self.context.socket(zmq.PULL)
        self.pull_socket.connect(f"tcp://localhost:{self.port2}")
        self.poller = zmq.Poller()
        self.poller.register(self.pull_socket, zmq.POLLIN)
        self.encoder = msgpack.Encoder()
        self.decoder = msgpack.Decoder(DetokenizerOutputs)
        self.detokenizer_reqs: Dict[str, Request] = {}

        self.generation_config_fields = _load_generation_config_dict(
            model_config)
        self.input_preprocessor = InputPreprocessor(model_config,
                                                    self.tokenizer)
        self.input_registry = input_registry
        self.input_processor = input_registry.create_input_processor(
            model_config)

        self.model_executor = GPUExecutor(
            model_config=model_config,
            cache_config=cache_config,
            parallel_config=parallel_config,
            scheduler_config=scheduler_config,
            device_config=device_config,
            lora_config=lora_config,
            speculative_config=speculative_config,
            load_config=load_config,
            prompt_adapter_config=prompt_adapter_config,
            observability_config=self.observability_config,
        )
        assert not self.model_config.embedding_mode
        self._initialize_kv_caches()

        # Create the scheduler.
        # NOTE: the cache_config here have been updated with the numbers of
        # GPU and CPU blocks, which are profiled in the distributed executor.
        self.scheduler = Scheduler(scheduler_config, cache_config, lora_config)

    def _initialize_kv_caches(self) -> None:
        num_gpu_blocks, _ = self.model_executor.determine_num_available_blocks(
        )

        if self.cache_config.num_gpu_blocks_override is not None:
            num_gpu_blocks_override = self.cache_config.num_gpu_blocks_override
            logger.info(
                "Overriding num_gpu_blocks=%d with "
                "num_gpu_blocks_override=%d", num_gpu_blocks,
                num_gpu_blocks_override)
            num_gpu_blocks = num_gpu_blocks_override

        self.cache_config.num_gpu_blocks = num_gpu_blocks
        self.cache_config.num_cpu_blocks = 0
        self.model_executor.initialize_cache(num_gpu_blocks)

    @classmethod
    def from_engine_args(
        cls,
        engine_args: EngineArgs,
        usage_context: UsageContext = UsageContext.ENGINE_CONTEXT,
        stat_loggers: Optional[Dict[str, StatLoggerBase]] = None,
    ) -> "LLMEngine":
        """Creates an LLM engine from the engine arguments."""
        # Create the engine configs.
        engine_config = engine_args.create_engine_config()
        # Create the LLM engine.
        engine = cls(
            **engine_config.to_dict(),
            log_stats=not engine_args.disable_log_stats,
            usage_context=usage_context,
            stat_loggers=stat_loggers,
        )
        return engine

    def _init_tokenizer(self) -> BaseTokenizerGroup:
        return init_tokenizer_from_configs(
            model_config=self.model_config,
            scheduler_config=self.scheduler_config,
            parallel_config=self.parallel_config,
            enable_lora=bool(self.lora_config))

    def _verify_args(self) -> None:
        self.model_config.verify_with_parallel_config(self.parallel_config)
        self.cache_config.verify_with_parallel_config(self.parallel_config)
        if self.lora_config:
            self.lora_config.verify_with_model_config(self.model_config)
            self.lora_config.verify_with_scheduler_config(
                self.scheduler_config)
        if self.prompt_adapter_config:
            self.prompt_adapter_config.verify_with_model_config(
                self.model_config)

    def _add_processed_request(
        self,
        request_id: str,
        processed_inputs: Union[DecoderOnlyInputs, EncoderDecoderLLMInputs],
        params: Union[SamplingParams, PoolingParams],
        arrival_time: float,
        lora_request: Optional[LoRARequest],
        prompt_adapter_request: Optional[PromptAdapterRequest],
        trace_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._validate_model_inputs(processed_inputs)
        eos_token_id = self.input_preprocessor.get_eos_token_id(lora_request)

        # TODO(woosuk): Support embedding mode.
        assert isinstance(params, SamplingParams)
        sampling_params = params.clone()
        sampling_params.update_from_generation_config(
            self.generation_config_fields, eos_token_id)

        # TODO(woosuk): Check max_logprobs
        # TODO(woosuk): Support encoder-decoder models.
        req = Request(request_id,
                      processed_inputs,
                      arrival_time,
                      sampling_params=params)
        self.scheduler.add_request(req)

    def stop_remote_worker_execution_loop(self) -> None:
        self.model_executor.stop_remote_worker_execution_loop()

    def add_request(
        self,
        request_id: str,
        prompt: PromptType,
        params: Union[SamplingParams, PoolingParams],
        arrival_time: Optional[float] = None,
        lora_request: Optional[LoRARequest] = None,
        trace_headers: Optional[Mapping[str, str]] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
        priority: int = 0,
    ) -> None:
        if lora_request is not None and not self.lora_config:
            raise ValueError(f"Got lora_request {lora_request} but LoRA is "
                             "not enabled!")
        if arrival_time is None:
            arrival_time = time.time()
        assert priority == 0, "vLLM V1 does not support priority at the moment."

        preprocessed_inputs = self.input_preprocessor.preprocess(
            prompt,
            request_id=request_id,
            lora_request=lora_request,
            prompt_adapter_request=prompt_adapter_request,
        )
        processed_inputs = self.input_processor(preprocessed_inputs)

        self._add_processed_request(
            request_id=request_id,
            processed_inputs=processed_inputs,
            params=params,
            arrival_time=arrival_time,
            lora_request=lora_request,
            prompt_adapter_request=prompt_adapter_request,
            trace_headers=trace_headers,
        )

    def abort_request(self, request_id: Union[str, Iterable[str]]) -> None:
        self.scheduler.abort_requests(request_id)

    def get_num_unfinished_requests(self) -> int:
        """Gets the number of unfinished requests."""
        # FIXME(woosuk)
        return self.scheduler.get_num_unfinished_requests()

    def has_unfinished_requests(self) -> bool:
        """Returns True if there are unfinished requests."""
        # FIXME(woosuk)
        return (self.scheduler.has_unfinished_requests()
                or len(self.detokenizer_reqs) > 0)

    def step(self) -> Tuple[List[Request], List[Request]]:
        if self.scheduler.has_unfinished_requests():
            scheduler_output = self.scheduler.schedule()
            output = self.model_executor.execute_model(scheduler_output)
            finished_reqs = self.scheduler.update_from_output(
                scheduler_output, output)

            if finished_reqs:
                self.send_to_detokenizer(finished_reqs)
        detokenized_reqs = self.recv_from_detokenizer()
        return detokenized_reqs, self.scheduler.running

    def send_to_detokenizer(self, requests: List[Request]) -> None:
        inputs = DetokenizerInputs(
            req_ids=[],
            prompt_token_ids=[],
            new_token_ids=[],
            skip_special_tokens=[],
            spaces_between_special_tokens=[],
            free_req_ids=[],
        )
        for req in requests:
            self.detokenizer_reqs[req.request_id] = req
            inputs.req_ids.append(req.request_id)
            inputs.prompt_token_ids.append(req.prompt_token_ids)
            inputs.new_token_ids.append(req.output_token_ids)
            inputs.skip_special_tokens.append(
                req.sampling_params.skip_special_tokens)
            inputs.spaces_between_special_tokens.append(
                req.sampling_params.spaces_between_special_tokens)
        self.push_socket.send(self.encoder.encode(inputs), flags=zmq.NOBLOCK)

    def recv_from_detokenizer(self) -> List[Request]:
        detokenized_reqs: List[Request] = []
        socks = dict(self.poller.poll(timeout=0))
        if self.pull_socket in socks and socks[self.pull_socket] == zmq.POLLIN:
            msg = self.pull_socket.recv()
            data = self.decoder.decode(msg)
            num_reqs = len(data.req_ids)
            for i in range(num_reqs):
                req_id = data.req_ids[i]
                assert req_id in self.detokenizer_reqs
                req = self.detokenizer_reqs.pop(req_id)
                req.output_text += data.detokenized_texts[i]
                detokenized_reqs.append(req)
        return detokenized_reqs

    def terminate_detokenizer(self) -> None:
        self.push_socket.send(b"", flags=zmq.NOBLOCK)

    def check_health(self) -> None:
        if self.tokenizer:
            self.tokenizer.check_health()
        self.model_executor.check_health()

    def _validate_model_inputs(self, inputs: Union[DecoderOnlyInputs,
                                                   EncoderDecoderLLMInputs]):
        prompt_ids = inputs.get("prompt_token_ids")
        if prompt_ids is None or len(prompt_ids) == 0:
            raise ValueError("Prompt cannot be empty")

        if self.model_config.is_multimodal_model:
            max_prompt_len = self.model_config.max_model_len

            if len(prompt_ids) > max_prompt_len:
                raise ValueError(
                    f"The prompt (total length {len(prompt_ids)}) is too long "
                    f"to fit into the model (context length {max_prompt_len}). "
                    "Make sure that `max_model_len` is no smaller than the "
                    "number of text tokens plus multimodal tokens. For image "
                    "inputs, the number of image tokens depends on the number "
                    "of images, and possibly their aspect ratios as well.")

    def get_model_config(self) -> ModelConfig:
        """Gets the model configuration."""
        return self.model_config

    def get_parallel_config(self) -> ParallelConfig:
        """Gets the parallel configuration."""
        return self.parallel_config

    def get_decoding_config(self) -> DecodingConfig:
        """Gets the decoding configuration."""
        return self.decoding_config

    def get_scheduler_config(self) -> SchedulerConfig:
        """Gets the scheduler configuration."""
        return self.scheduler_config

    def get_lora_config(self) -> LoRAConfig:
        """Gets the LoRA configuration."""
        return self.lora_config


def _load_generation_config_dict(model_config: ModelConfig) -> Dict[str, Any]:
    config = try_get_generation_config(
        model_config.model,
        trust_remote_code=model_config.trust_remote_code,
        revision=model_config.revision,
    )

    if config is None:
        return {}

    return config.to_diff_dict()