import logging

# TODO change this to be something can be configured?
LOG_FILE = "logs/prunning.log"


def configure_logger():
    logger = logging.getLogger()
    logger.setLevel(
        logging.DEBUG
    )  # Set the logger level to the lowest level you want to capture

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


logger = configure_logger()
