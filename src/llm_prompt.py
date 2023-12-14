import logging
import openai
import os
import tiktoken
from dafny_utils import extract_dafny_functions

openai.api_key = os.getenv("OPENAI_API_KEY")

logger = logging.getLogger(__name__)


class Llm_prompt:
    def __init__(self, system_prompt, context):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for examples in context:
            with open(examples["File_to_fix"], "r") as f:
                # remove the last line since it is the code url
                # TODO clean that
                code_to_fix = "\n".join(f.read().split("\n")[:-1])
            user_content = f"{examples['Question_prompt']} {code_to_fix}"

            with open(examples["Fix"], "r") as f:
                # remove the last line since it is the code url
                # TODO clean that
                fix = "\n".join(f.read().split("\n")[:-1])
            assistant_content = f"{examples['Answer_prompt']} {fix}"

            messages.append({"role": "user", "content": user_content})
            messages.append({"role": "assistant", "content": assistant_content})
        self.messages = messages

    def add_question(
        self,
        program_to_fix,
        method_name,
        fix_prompt,
        model_parameters,
        context_option,
        feedback,
    ):
        with open(program_to_fix, "r") as f:
            content = f.read()
        method = extract_dafny_functions(content, method_name)
        context = content.replace(method, "")

        # current size =
        current_prompt_length = self.get_prompt_length(model_parameters["Encoding"])
        encoding = tiktoken.get_encoding(model_parameters["Encoding"])
        num_tokens_method = len(encoding.encode(method))
        num_tokens_fix_prompt = len(encoding.encode(fix_prompt))
        current_prompt_length += num_tokens_method + num_tokens_fix_prompt
        # depending on feedback included add the feedback
        question = f"{fix_prompt} {method_name} {method}"
        if feedback:
            num_tokens_feedback = len(encoding.encode(feedback))
            current_prompt_length += num_tokens_feedback
            question += f"\n Error Message given by Dafny: {feedback}"
            logger.debug(f"Feedback added: {num_tokens_feedback}")

        # if context enabled:
        if context_option == "File":
            encoded_context = encoding.encode(context)
            num_tokens_context = len(encoded_context)
            if (
                current_prompt_length
                + num_tokens_context
                + model_parameters["Max_tokens"]
                + 100
                > model_parameters["Prompt_limit"]
            ):
                # cut the context up to the limit
                token_limit = (
                    model_parameters["Prompt_limit"]
                    - current_prompt_length
                    - model_parameters["Max_tokens"]
                    - 100
                )
                encoded_context = encoded_context[:token_limit]
                context = encoding.decode(encoded_context)
                question += f"\n Context of the method: \n {context}"
        self.messages.append({"role": "user", "content": question})

    def save_prompt(self, path):
        with open(path, "w") as f:
            for message in self.messages:
                f.write(f"{message['role']}: {message['content']}\n")

    def get_prompt_length(self, encoding_name):
        encoding = tiktoken.get_encoding(encoding_name)
        content_string = "".join(item["content"] for item in self.messages)
        num_tokens = len(encoding.encode(content_string))
        return num_tokens

    def generate_fix(self, model_parameters):
        response = openai.ChatCompletion.create(
            model=model_parameters["Model"],
            temperature=model_parameters["Temperature"],
            max_tokens=model_parameters["Max_tokens"],
            messages=self.messages,
        )
        return response
