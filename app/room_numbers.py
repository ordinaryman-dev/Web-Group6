import re


def floor_key(number):
    match = re.fullmatch(r"([0-9]+)([0-9]{2})", number or "")
    if match:
        return match.group(1)
    match = re.match(r"([A-Za-z]+)", number or "")
    return match.group(1).upper() if match else "OTHER"


def floor_label(key):
    return f"{int(key)}F" if key.isdigit() else key


def floor_sort(key):
    return (0, int(key), "") if key.isdigit() else (1, 0, key)
