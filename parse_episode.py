import re

def parse_ep(text):
    match = re.search(r'(?:E?(\d{1,3})(?:[^\d]+E?(\d{1,3}))?)', text, re.IGNORECASE)
    if match:
        first = int(match.group(1))
        if match.group(2):
            second = int(match.group(2))
            return [first, second]
        return first
    return None

print(parse_ep("E01E02"))
print(parse_ep("01-02"))
print(parse_ep("01"))
print(parse_ep("E01"))
print(parse_ep("1-2"))
print(parse_ep("e1e2"))
