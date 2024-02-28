import logging
import os
import shutil
import traceback
import urllib.parse
from llm_prompt import Llm_prompt
from dafny_utils import (
    extract_dafny_functions,
)
from config_parsing import parse_config_llm
from error_parser import remove_warning
from google_uploader import upload_results
from utils import (
    write_csv_header_arg,
    extract_string_between_backticks,
    read_pruning_result,
)

from Method import Method

logger = logging.getLogger(__name__)

SERVER_NAME = "http://c10-09.sysnet.ucsd.edu:8866/"


def generate_fix_llm(config_file, pruning_file=None):
    if pruning_file:
        return handle_pruning_results(config_file, pruning_file)
    else:
        return handle_no_pruning_results(config_file)


def generate_notebook_url(result_file, assertion_file, method_index):
    print(result_file)
    print(assertion_file)
    print(method_index)
    query_params = {
        "results": result_file,
        "assertions": assertion_file,
        "method": method_index,
    }

    encoded_params = urllib.parse.urlencode(query_params)

    full_url = SERVER_NAME + "?" + encoded_params
    print(full_url)
    return full_url


def handle_pruning_results(config_file, pruning_file):
    pruning_results = read_pruning_result(pruning_file)
    _, config = parse_config_llm(config_file)
    method_processed, success_count = 0, 0

    header = [
        "Index",
        "Original Method File",
        "Original Method",
        "Original Method Time",
        "Original Method Result",
        "Original Result File",
        "Original Error Message",
        "Original Error Message File",
        "New Method File",
        "New Method",
        "New Method Time",
        "New Method Result",
        "New Method Result File",
        "New Method Error Message",
        "New Method Error Message File",
        "Prompt File",
        "Prompt Length",
        "Prompt Index",
        "Prompt_name",
        "Diff",
        "Url",
    ]
    csv_writer, file = write_csv_header_arg(config["Results_file"], header)
    for row in pruning_results:
        # if method_processed != 7:
        #     method_processed += 1
        #     continue
        # pass
        # break
        method, tmp_original_file_location = setup_verification_environment(
            config, row, method_processed
        )
        try:
            (
                new_method,
                prompt_path,
                prompt_length,
                diff,
                prompt_index,
                prompt_name,
                success,
            ) = process_method(method, config, method_processed, row["Original File"])
            notebook_url = generate_notebook_url(
                config["Results_file"], pruning_file, method_processed
            )
            success_count += (
                1
                if store_results_and_compare(
                    method_processed,
                    method,
                    new_method,
                    config,
                    row["New Method File"],
                    prompt_path,
                    prompt_length,
                    diff,
                    prompt_index,
                    prompt_name,
                    notebook_url,
                    csv_writer=csv_writer,
                )
                else 0
            )
        except Exception as e:
            traceback_str = traceback.format_exc()
            logger.error(f"An error occurred: {e}\n{traceback_str}")
        cleanup_environment(tmp_original_file_location, row["Original File"])
        logger.info(f"Success rate: {success_count}/{method_processed}")
        method_processed += 1

    file.close()
    upload_results("", config["Results_file"])
    return success_count, method_processed


def handle_no_pruning_results(config_file):
    methods, config = parse_config_llm(config_file)
    method_processed, success_count = 0, 0

    for method in methods:
        original_file_location = method.file_path
        method_processed += 1
        (
            new_method,
            prompt_path,
            prompt_length,
            diff,
            prompt_index,
            prompt_name,
        ) = process_method(method, config, method_processed, original_file_location)
        success_count += (
            1
            if store_results_and_compare(
                method,
                new_method,
                config,
                method.file_path,
                prompt_path,
                prompt_length,
                diff,
                prompt_index,
                prompt_name,
            )
            else 0
        )
        shutil.copy(method.file_path, original_file_location)
        logger.info(f"Success rate: {success_count}/{method_processed}")

    return success_count, method_processed


def process_method(method, config, index, original_file_location):
    logger.info("+--------------------------------------+")
    method.run_verification(config["Results_dir"], config.get("Dafny_args", ""))
    logger.debug(method)
    for prompt_index, config_prompt in enumerate(config["Prompts"], start=1):
        for i in range(config_prompt["Nb_tries"]):
            try:
                llm_prompt = Llm_prompt(
                    config_prompt["System_prompt"], config_prompt["Context"]
                )
                llm_prompt.add_question(
                    method.file_path,
                    method.method_name,
                    method.entire_error_message,
                    config_prompt["Fix_prompt"],
                    config["Model_parameters"],
                    config_prompt["Method_context"],
                    method.entire_error_message if config_prompt["Feedback"] else None,
                )
                prompt_path = f"{method.file_path}_{index}_prompt"
                llm_prompt.save_prompt(prompt_path)
                prompt_length = llm_prompt.get_prompt_length(
                    config["Model_parameters"]["Encoding"]
                )
                logger.info(f"Prompt length: {prompt_length}")

                logger.info(f"{method.method_name} ===> Try {i+1}")
                response = llm_prompt.generate_fix(
                    config["Model_parameters"],
                )
                fix_prompt = response
                logger.info(fix_prompt)
                code = extract_string_between_backticks(fix_prompt)
                new_method_content = get_new_method_content(
                    code if code else fix_prompt, method.method_name
                )
                diff = method.get_diff(new_method_content)
                new_method = method.create_modified_method(
                    new_method_content, os.path.dirname(method.file_path), index, "fix"
                )
                logger.debug(f"diff : {diff}")
                method.move_to_results_directory(config["Results_dir"])
                new_method.run_verification(
                    config["Results_dir"], config.get("Dafny_args", "")
                )
                previous_error = remove_warning(method.entire_error_message)
                new_error = ""
                if new_method.entire_error_message is not None:
                    new_error = remove_warning(new_method.entire_error_message)
                if new_method.verification_result == "Correct":
                    logger.info(f"Success with prompt {prompt_index} on try {i}")
                    return (
                        new_method,
                        prompt_path,
                        prompt_length,
                        diff,
                        prompt_index,
                        config_prompt["Prompt_name"],
                        True,
                    )
                elif previous_error != new_error and config_prompt["Error_feedback"]:
                    # reset env
                    new_method.move_to_results_directory(config["Results_dir"])
                    shutil.copy(method.file_path, original_file_location)
                    method.file_path = original_file_location

                    llm_prompt.feedback_error_message(new_error)
                    llm_prompt.save_prompt(prompt_path)
                    response = llm_prompt.generate_fix(
                        config["Model_parameters"],
                    )
                    fix_prompt = response
                    logger.info(fix_prompt)
                    code = extract_string_between_backticks(fix_prompt)
                    new_method_content = get_new_method_content(
                        code if code else fix_prompt, method.method_name
                    )
                    diff = method.get_diff(new_method_content)
                    new_method = method.create_modified_method(
                        new_method_content,
                        os.path.dirname(method.file_path),
                        index,
                        "fix",
                    )
                    logger.debug(f"diff : {diff}")
                    method.move_to_results_directory(config["Results_dir"])
                    new_method.run_verification(
                        config["Results_dir"], config.get("Dafny_args", "")
                    )
                    if new_method.verification_result == "Correct":
                        logger.info(f"Success with prompt {prompt_index} on try {i}")
                        return (
                            new_method,
                            prompt_path,
                            prompt_length,
                            diff,
                            prompt_index,
                            config_prompt["Prompt_name"],
                            True,
                        )
                if i + 1 != config_prompt["Nb_tries"]:
                    new_method.move_to_results_directory(config["Results_dir"])
                    shutil.copy(method.file_path, original_file_location)
                    method.file_path = original_file_location
            except Exception as e:
                method.move_to_results_directory(config["Results_dir"])
                traceback_str = traceback.format_exc()
                logger.error(f"An error occurred: {e}\n{traceback_str}")
        return (
            new_method,
            prompt_path,
            prompt_length,
            diff,
            prompt_index,
            config_prompt["Prompt_name"],
            False,
        )


def setup_verification_environment(config, row, index=0):
    original_filepath = row["Original File"]
    tmp_original_file_location = shutil.move(
        original_filepath,
        os.path.join(config["Results_dir"], os.path.basename(original_filepath)),
    )
    original_method_file = os.path.join(
        os.path.dirname(original_filepath), os.path.basename(row["New Method File"])
    )
    if row["New Method File"] != original_method_file:
        shutil.copy(
            row["New Method File"],
            # os.path.join(config["Results_dir"], os.path.basename(row["New Method File"])),
            original_method_file,
        )
    return (
        Method(original_method_file, row["Original Method"], index=index),
        tmp_original_file_location,
    )


def get_new_method_content(fix_prompt, method_name):
    new_method_content = extract_dafny_functions(fix_prompt, method_name)
    new_method_content = "\n".join(
        line.lstrip("+") for line in new_method_content.splitlines()
    )
    return new_method_content


def cleanup_environment(tmp_original_file_location, original_file_path):
    shutil.copy(tmp_original_file_location, original_file_path)


def store_results_and_compare(
    method_index,
    method,
    new_method,
    config,
    original_file,
    prompt_path,
    prompt_length,
    diff,
    prompt_index,
    prompt_name,
    notebook_url,
    csv_writer=None,
):
    comparison_result, comparison_details = method.compare(new_method)
    new_method.move_to_results_directory(config["Results_dir"])
    logger.info(comparison_details)
    fix_stats = [
        method_index,
        original_file,
        method.method_name,
        method.verification_time,
        method.verification_result,
        method.dafny_log_file,
        method.error_message,
        method.error_file_path,
        new_method.file_path,
        new_method.method_name,
        new_method.verification_time,
        new_method.verification_result,
        new_method.dafny_log_file,
        new_method.error_message,
        new_method.error_file_path,
        prompt_path,
        prompt_length,
        prompt_index,
        prompt_name,
        diff if len(diff) > 5 else "",
        notebook_url,
    ]
    if csv_writer:
        csv_writer.writerow(fix_stats)
    return comparison_result
