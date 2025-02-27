import copy
import logging
import json
import openai
import tiktoken
import yaml

from dafny_utils import (
    extract_dafny_functions,
    remove_line_numbers,
    replace_and_extract_method_with_line_numbers,
)
from guidance import system, user, assistant, models

from error_parser import insert_assertion_location
from utils import string_difference


logger = logging.getLogger(__name__)


class Llm_prompt:
    def __init__(self, index, system_prompt, example_selector):
        with open(".secrets.yaml", "r") as f:
            secrets = yaml.safe_load(f)
        openai.api_key = secrets["OPENAI_API_KEY"]
        model = models.OpenAI("gpt-4o", echo=False, api_key=secrets["OPENAI_API_KEY"])
        messages = []
        chat = model
        if system_prompt:
            with system():
                messages.append({"role": "system", "content": system_prompt})
                chat += system_prompt
        if (
            example_selector is not None
            and example_selector.nature != "Dynamic"
            and example_selector.nature != "Embedding"
            and example_selector.nature != "TFIDF"
        ):
            for example in example_selector.examples:
                with user():
                    chat += example["Question"]
                with assistant():
                    chat += example["Answer"]
                messages.append({"role": "user", "content": example["Question"]})
                messages.append({"role": "assistant", "content": example["Answer"]})

        self.chat = chat
        self.messages = messages
        self.index = index

    def copy(self):
        # Manually copy the attributes because
        # a deepcopy would not work with guidance
        new_prompt = Llm_prompt.__new__(Llm_prompt)
        with open(".secrets.yaml", "r") as f:
            secrets = yaml.safe_load(f)
        model = models.OpenAI("gpt-4", echo=False, api_key=secrets["OPENAI_API_KEY"])
        chat = model
        new_prompt.messages = copy.deepcopy(self.messages)
        for message in new_prompt.messages:
            if message["role"] == "user":
                with user():
                    chat += message["content"]
            elif message["role"] == "assistant":
                with assistant():
                    chat += message["content"]
            elif message["role"] == "system":
                with system():
                    chat += message["content"]
        new_prompt.chat = chat
        new_prompt.index = self.index
        return new_prompt

    def remove_answer(self, method_content, method_name):
        messages_copy = self.messages.copy()
        for i, message in enumerate(messages_copy):
            if message["role"] == "user":
                extracted_method = extract_dafny_functions(
                    message["content"], method_name
                )
                if extracted_method is not None:
                    diff = string_difference(method_content, extracted_method)
                    diff = diff.replace(
                        "<assertion> Insert the assertion here </assertion>\n", ""
                    )
                    self.messages[i]["content"] = self.messages[i]["content"].replace(
                        diff, ""
                    )

    def add_question(
        self,
        program_to_fix,
        method_name,
        error_message,
        model_parameters,
        config_prompt,
        feedback,
        example_selector,
        threshold,
        dafny_args,
        original_method_file,
    ):
        with open(program_to_fix, "r") as f:
            content = f.read()
        method = extract_dafny_functions(content, method_name)
        examples = []
        if example_selector.nature == "Dynamic":
            examples = example_selector.generate_dynamic_examples(
                method, threshold, config_prompt["Fix_prompt"], program_to_fix
            )
        elif example_selector.nature == "Embedding":
            examples = example_selector.generate_embedded_examples(
                method, threshold, config_prompt["Fix_prompt"], program_to_fix
            )
        elif example_selector.nature == "TFIDF":
            examples = example_selector.generate_tfidf_examples(
                method, threshold, config_prompt["Fix_prompt"], program_to_fix
            )
        for example in examples:
            with user():
                self.chat += example["Question"]
            with assistant():
                self.chat += example["Answer"]
            self.messages.append({"role": "user", "content": example["Question"]})
            self.messages.append({"role": "assistant", "content": example["Answer"]})
        # Everything but the method
        self.remove_answer(method, method_name)
        context = content.replace(method, "")

        current_prompt_length = self.get_prompt_length(model_parameters["Encoding"])
        encoding = tiktoken.get_encoding(model_parameters["Encoding"])
        num_tokens_method = len(encoding.encode(method))
        num_tokens_fix_prompt = len(encoding.encode(config_prompt["Fix_prompt"]))
        current_prompt_length += num_tokens_method + num_tokens_fix_prompt

        method_to_insert = method
        multiple_locations = False
        if (
            "Multiple_locations" in config_prompt
            and config_prompt["Multiple_locations"]
        ):
            multiple_locations = True
        if config_prompt["Placeholder"]:
            if "--library" in dafny_args:
                # extract the files url after --library in such a string "--library /usr/local/home/eric/dafny_repos/Dafny-VMC/src/**/*.dfy --resource-limit 20000"
                library_files = dafny_args.split("--library")[1].split("--")[0].strip()
                method_to_insert = insert_assertion_location(
                    error_message,
                    program_to_fix,
                    method_name,
                    optional_files=library_files,
                    original_method_file=original_method_file,
                    multiple_locations=multiple_locations,
                )
            else:
                method_to_insert = insert_assertion_location(
                    error_message,
                    program_to_fix,
                    method_name,
                    multiple_locations=multiple_locations,
                )
        fix_prompt = config_prompt["Fix_prompt"]
        method_to_insert = replace_and_extract_method_with_line_numbers(
            program_to_fix, method_to_insert, method_name
        )
        question = f"{fix_prompt}\n <method> {method_to_insert} </method>"
        if feedback:
            num_tokens_feedback = len(encoding.encode(feedback))
            current_prompt_length += num_tokens_feedback
            question += f"\n\n<error>\n{feedback}\n</error>"
            logger.debug(f"Feedback added: {num_tokens_feedback}")

        if config_prompt["Method_context"] == "File":
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

        with user():
            self.chat += question
        self.messages.append({"role": "user", "content": question})
        return remove_line_numbers(method_to_insert)

    def feedback_error_message(self, error_message):
        error_feedback = (
            "This is the new error message that we get after the indicated change:\n <error>\n"
            + error_message
            + "\n <\\error>"
        )
        with user():
            self.chat += error_feedback
        self.messages.append({"role": "user", "content": error_feedback})

    def set_path(self, path):
        self.path = path

    def save_prompt(self):
        if not hasattr(self, "path"):
            raise ValueError("Prompt path not set")
        with open(self.path, "w") as f:
            json.dump(self.messages, f)

    def get_prompt_length(self, encoding_name):
        encoding = tiktoken.get_encoding(encoding_name)
        content_string = "".join(item["content"] for item in self.messages)
        num_tokens = len(encoding.encode(content_string))
        return num_tokens

    def get_latest_message(self):
        return self.messages[-1]

    def get_n_fixes(self, model_parameters, n, placeholder):
        fixes = self.generate_n_fix(model_parameters, n, placeholder)
        duplicated_prompts = [self.copy() for _ in range(n - 1)]
        duplicated_prompts.append(self)
        for i, prompt in enumerate(duplicated_prompts):
            prompt.messages.append({"role": "assistant", "content": str(fixes[i])})
        return duplicated_prompts

    def get_fix(self, model_parameters, placeholder):
        fix = self.generate_fix(model_parameters, placeholder)
        self.messages.append({"role": "assistant", "content": str(fix)})
        return fix

    def generate_n_fix(self, model_parameters, n, placeholder):
        tools_line = [
            {
                "type": "function",
                "function": {
                    "name": "insert_dafny_assertion",
                    "description": "Use this function to insert a Dafny assertion",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "assertion": {
                                "type": "string",
                                "description": "The Dafny assertion to insert into the placeholder. It needs to start with `assert`.",
                            },
                            "location": {
                                "type": "number",
                                "description": "The line number in which to insert the assertion. (from 1 to n, n being the total lines)",
                            },
                        },
                        "required": ["assertion", "location"],
                    },
                },
            }
        ]
        tools_placeholder = [
            {
                "type": "function",
                "function": {
                    "name": "insert_dafny_assertion",
                    "description": "Use this function to insert a Dafny assertion into a predefined placeholder.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "assertion": {
                                "type": "string",
                                "description": "The Dafny assertion to insert into the placeholder. It needs to start with `assert`.",
                            },
                            "location": {
                                "type": "number",
                                "description": "The placeholder number in which to insert the assertion. (from 1 to n, n being the total number of placeholders available)",
                            },
                        },
                        "required": ["assertion", "location"],
                    },
                },
            }
        ]
        response = openai.chat.completions.create(
            model=model_parameters["Model"],
            temperature=model_parameters["Temperature"],
            max_tokens=model_parameters["Max_tokens"],
            messages=self.messages,
            tools=tools_placeholder if placeholder else tools_line,
            tool_choice={
                "type": "function",
                "function": {"name": "insert_dafny_assertion"},
            },
            n=n,
        )

        generated_fixes = []
        for choice in response.choices:
            function_arguments = json.loads(
                choice.message.tool_calls[0].function.arguments
            )
            generated_fixes.append(
                (function_arguments["assertion"], function_arguments["location"])
            )

        return generated_fixes

    def generate_fix(self, model_parameters, placeholder):
        tools_line = [
            {
                "type": "function",
                "function": {
                    "name": "insert_dafny_assertion",
                    "description": "Use this function to insert a Dafny assertion",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "assertion": {
                                "type": "string",
                                "description": "The Dafny assertion to insert into the placeholder. It needs to start with `assert`.",
                            },
                            "location": {
                                "type": "number",
                                "description": "The line number in which to insert the assertion. (from 1 to n, n being the total lines)",
                            },
                        },
                        "required": ["assertion", "location"],
                    },
                },
            }
        ]
        tools_placeholder = [
            {
                "type": "function",
                "function": {
                    "name": "insert_dafny_assertion",
                    "description": "Use this function to insert a Dafny assertion into a predefined placeholder.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "assertion": {
                                "type": "string",
                                "description": "The Dafny assertion to insert into the placeholder. It needs to start with `assert`.",
                            },
                            "location": {
                                "type": "number",
                                "description": "The placeholder number in which to insert the assertion. (from 1 to n, n being the total number of placeholders available)",
                            },
                        },
                        "required": ["assertion", "location"],
                    },
                },
            }
        ]
        response = openai.chat.completions.create(
            model=model_parameters["Model"],
            temperature=model_parameters["Temperature"],
            max_tokens=model_parameters["Max_tokens"],
            messages=self.messages,
            tools=tools_placeholder if placeholder else tools_line,
            tool_choice={
                "type": "function",
                "function": {"name": "insert_dafny_assertion"},
            },
        )

        function_arguments = json.loads(
            response.choices[0].message.tool_calls[0].function.arguments
        )
        return function_arguments["assertion"], function_arguments["location"]
