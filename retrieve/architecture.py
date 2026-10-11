import openai
import torch
import transformers

class LLMChat():
    def __init__(self, model_name='meta-llama/Meta-Llama-3-8B-Instruct', backend='auto', temperature=0.0, timeout=None, max_retries=None):
        super(LLMChat, self).__init__()
        if backend not in ('auto', 'openai', 'transformers'):
            raise ValueError('unknown LLM backend')
        self.model_name = model_name
        self.backend = ('openai' if model_name.startswith('gpt') else 'transformers') if backend == 'auto' else backend
        self.temperature = temperature
        if self.backend == 'openai':
            options = {}
            if timeout is not None:
                options["timeout"] = timeout
            if max_retries is not None:
                options["max_retries"] = max_retries
            self.client = openai.OpenAI(**options)
        else:
            self.pipeline = transformers.pipeline(
                "text-generation",
                model=model_name,
                model_kwargs={"torch_dtype": torch.bfloat16},
                device="cuda",
            )

    def generate(self, messages, max_new_tokens = 500):
        if self.backend == 'openai':
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=self.temperature,
            )
            return response.choices[0].message.content
        else:
            outputs = self.pipeline(
                messages,
                max_new_tokens=max_new_tokens,
                do_sample=False
            )
            return outputs[0]["generated_text"][-1]
