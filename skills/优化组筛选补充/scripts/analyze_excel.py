#!/usr/bin/env python3
"""
从本地 Excel ad group export 文件中：
1. 归一化 top 语言输入，输出供用户确认的候选识别标记
2. 提取前20条数据并识别语言
3. 对比 top 语言列表，找出缺失语言
4. 从第21条数据起，为每个缺失语言搜索首条命中的广告组
输出 JSON 到 stdout
"""
import argparse
import json
import re
import sys

try:
    import openpyxl
except ImportError:
    print("请先安装依赖：pip install openpyxl", file=sys.stderr)
    sys.exit(1)


LANG_SPECS = {
    "en": {
        "display_name": "英语",
        "input_aliases": ["英语", "英文", "en", "english"],
        "match_tokens": ["en", "english"],
    },
    "es": {
        "display_name": "西语",
        "input_aliases": ["西语", "西班牙语", "es", "spanish", "es-mx", "esmx"],
        "match_tokens": ["es", "spanish", "es-mx"],
    },
    "pt": {
        "display_name": "葡语",
        "input_aliases": [
            "葡语",
            "葡萄牙语",
            "pt",
            "portuguese",
            "pt-br",
            "ptbr",
            "pt_br",
            "brazilian portuguese",
            "巴西葡语",
            "葡萄牙语巴西",
            "葡萄牙语（巴西）",
        ],
        "match_tokens": ["pt", "portuguese", "pt_br", "pt-br", "ptbr", "brazilian portuguese"],
    },
    "fr": {
        "display_name": "法语",
        "input_aliases": ["法语", "fr", "french"],
        "match_tokens": ["fr", "french"],
    },
    "ko": {
        "display_name": "韩语",
        "input_aliases": ["韩语", "韩文", "ko", "kr", "korean"],
        "match_tokens": ["ko", "kr", "korean"],
    },
    "tr": {
        "display_name": "土耳其语",
        "input_aliases": ["土耳其语", "tr", "turkish", "turk"],
        "match_tokens": ["tr", "turkish", "turk"],
    },
    "id": {
        "display_name": "印尼语",
        "input_aliases": ["印尼语", "印度尼西亚语", "id", "indonesian", "indo", "in_id", "in-id"],
        "match_tokens": ["id", "indonesian", "indo", "in_id", "in-id"],
    },
    "ar": {
        "display_name": "阿拉伯语",
        "input_aliases": ["阿拉伯语", "ar", "arabic", "arab"],
        "match_tokens": ["ar", "arabic", "arab"],
    },
    "fa": {
        "display_name": "波斯语",
        "input_aliases": ["波斯语", "波斯文", "fa", "persian", "farsi", "persian iran", "farsi iran"],
        "match_tokens": ["fa", "persian", "farsi"],
    },
    "de": {
        "display_name": "德语",
        "input_aliases": ["德语", "de", "german"],
        "match_tokens": ["de", "german"],
    },
    "it": {
        "display_name": "意大利语",
        "input_aliases": ["意大利语", "意语", "it", "italian"],
        "match_tokens": ["it", "italian"],
    },
    "pl": {
        "display_name": "波兰语",
        "input_aliases": ["波兰语", "pl", "polish"],
        "match_tokens": ["pl", "polish"],
    },
    "ja": {
        "display_name": "日语",
        "input_aliases": ["日语", "日文", "ja", "jp", "japanese"],
        "match_tokens": ["ja", "jp", "japanese"],
    },
    "ms": {
        "display_name": "马来语",
        "input_aliases": ["马来语", "马来文", "ms", "malay", "bahasa melayu", "bahasa malaysia", "bm"],
        "match_tokens": ["ms", "malay", "bahasa", "bm"],
    },
    "th": {
        "display_name": "泰语",
        "input_aliases": ["泰语", "泰文", "th", "thai"],
        "match_tokens": ["th", "thai"],
    },
    "ru": {
        "display_name": "俄语",
        "input_aliases": ["俄语", "俄文", "ru", "russian"],
        "match_tokens": ["ru", "russian"],
    },
    "nl": {
        "display_name": "荷兰语",
        "input_aliases": ["荷兰语", "nl", "dutch", "netherlands", "holland"],
        "match_tokens": ["nl", "dutch"],
    },
    "da": {
        "display_name": "丹麦语",
        "input_aliases": ["丹麦语", "da", "danish", "denmark"],
        "match_tokens": ["da", "danish"],
    },
}

def normalize_token(value: str) -> str:
    return re.sub(r"[\s_\-()（）,]+", "", value.strip().lower())


ALIAS_TO_LANG = {}
for lang_key, spec in LANG_SPECS.items():
    for alias in spec["input_aliases"]:
        ALIAS_TO_LANG[normalize_token(alias)] = lang_key
    for token in spec["match_tokens"]:
        ALIAS_TO_LANG[normalize_token(token)] = lang_key


def normalize_top_languages(top_lang_inputs):
    confirmed_languages = []
    unknown_inputs = []
    seen = set()
    for raw in top_lang_inputs:
        raw_text = str(raw).strip()
        if not raw_text:
            continue
        lang_key = analyze_top_language_input(raw_text)
        if not lang_key:
            unknown_inputs.append(raw_text)
            continue
        if lang_key in seen:
            continue
        spec = LANG_SPECS[lang_key]
        confirmed_languages.append(
            {
                "input_label": raw_text,
                "lang_key": lang_key,
                "lang": spec["display_name"],
                "match_tokens": list(spec["match_tokens"]),
            }
        )
        seen.add(lang_key)
    return {"confirmed_languages": confirmed_languages, "unknown_inputs": unknown_inputs}


def extract_language_region_parts(raw_text):
    match = re.match(r"^(.*?)\s*[\(\[]\s*([^()\[\]]+?)\s*[\)\]]\s*$", raw_text.strip())
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()


def analyze_top_language_input(raw_text):
    direct_lang_key = ALIAS_TO_LANG.get(normalize_token(raw_text))
    if direct_lang_key:
        return direct_lang_key

    base_part, region_part = extract_language_region_parts(raw_text)
    if not base_part:
        return None

    lang_key = ALIAS_TO_LANG.get(normalize_token(base_part))
    if not lang_key:
        return None
    return lang_key


def build_confirmation_preview(top_lang_inputs):
    normalized = normalize_top_languages(top_lang_inputs)
    return {
        "requested_top_languages": list(top_lang_inputs),
        "confirmed_languages": normalized["confirmed_languages"],
        "unknown_inputs": normalized["unknown_inputs"],
    }


def match_name_by_lang_key(name: str, lang_key: str):
    if not name:
        return None
    normalized_tokens = extract_name_tokens(name)
    normalized_name = " ".join(normalized_tokens)
    for token in LANG_SPECS[lang_key]["match_tokens"]:
        token_lower = token.lower()
        if " " in token_lower:
            if token_lower in normalized_name:
                return token_lower
            continue
        if "_" in token_lower or "-" in token_lower:
            if token_lower in normalized_tokens:
                return token_lower
            continue
        if len(token_lower) <= 3 and token_lower.isalpha():
            if token_lower in normalized_tokens:
                return token_lower
            continue
        if token_lower in normalized_tokens:
            return token_lower
    return None


def extract_name_tokens(name: str):
    tokens = []
    seen = set()
    # 匹配首尾为字母数字的片段（内部允许连字符/下划线），避免把前导/尾随连字符带入 token
    for segment in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*[A-Za-z0-9]|[A-Za-z0-9]", name):
        seg = segment.lower()
        if seg not in seen:
            tokens.append(seg)
            seen.add(seg)
        # 含连字符/下划线的复合片段额外拆分子 token（保留原片段供 compound 匹配）
        if re.search(r"[-_]", segment):
            for part in re.split(r"[-_]+", segment):
                p = part.lower()
                if p and p not in seen:
                    tokens.append(p)
                    seen.add(p)
    return tokens


def detect_lang(name: str):
    if not name:
        return None
    for lang_key, spec in LANG_SPECS.items():
        matched_by = match_name_by_lang_key(name, lang_key)
        if matched_by:
            return {"lang_key": lang_key, "lang": spec["display_name"], "matched_by": matched_by}
    return None


def build_entry(rank, row, detected):
    ad_id = str(row[0]) if row[0] else ""
    ad_name = str(row[2]) if row[2] else ""
    good_best = row[3] if row[3] is not None else ""
    rate = str(row[5]) if row[5] is not None else ""
    return {
        "rank": rank,
        "id": ad_id,
        "name": ad_name,
        "good_best": good_best,
        "rate": rate,
        "lang": detected["lang"] if detected else "其他",
        "lang_key": detected["lang_key"] if detected else "",
        "matched_by": detected["matched_by"] if detected else "",
    }


def analyze_excel_file(file_path, top_lang_inputs):
    wb = openpyxl.load_workbook(file_path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    data = rows[1:]

    preview = build_confirmation_preview(top_lang_inputs)
    confirmed_languages = preview["confirmed_languages"]
    confirmed_keys = [item["lang_key"] for item in confirmed_languages]
    lang_by_key = {item["lang_key"]: item for item in confirmed_languages}

    top_selected = []
    top_excluded = []
    present_keys = set()

    for rank, row in enumerate(data[:20], 1):
        detected = detect_lang(str(row[2]) if row[2] else "")
        entry = build_entry(rank, row, detected)
        if detected and detected["lang_key"] in lang_by_key:
            present_keys.add(detected["lang_key"])
            top_selected.append(entry)
        else:
            top_excluded.append(entry)

    missing_keys = [lang_key for lang_key in confirmed_keys if lang_key not in present_keys]

    supplements = []
    for lang_key in missing_keys:
        found = False
        # 对每个缺失语言，仅按文件顺序取前20条数据之后的首条命中。
        for data_rank, row in enumerate(data[20:], 21):
            ad_name = str(row[2]) if row[2] else ""
            matched_by = match_name_by_lang_key(ad_name, lang_key)
            if matched_by:
                supplements.append(
                    {
                        "lang": lang_by_key[lang_key]["lang"],
                        "lang_key": lang_key,
                        "found": True,
                        "row_index": data_rank + 1,
                        "data_rank": data_rank,
                        "id": str(row[0]) if row[0] else "",
                        "name": ad_name,
                        "good_best": row[3] if row[3] is not None else "",
                        "rate": str(row[5]) if row[5] is not None else "",
                        "matched_by": matched_by,
                        "selection_rule": "first_match_after_top20",
                    }
                )
                found = True
                break
        if not found:
            supplements.append(
                {
                    "lang": lang_by_key[lang_key]["lang"],
                    "lang_key": lang_key,
                    "found": False,
                    "reason": "无该语言",
                    "selection_rule": "first_match_after_top20",
                }
            )

    return {
        "requested_top_languages": preview["requested_top_languages"],
        "confirmed_languages": confirmed_languages,
        "unknown_inputs": preview["unknown_inputs"],
        "top_selected": top_selected,
        "top_excluded": top_excluded,
        "present_langs": [lang_by_key[lang_key]["lang"] for lang_key in confirmed_keys if lang_key in present_keys],
        "present_lang_keys": [lang_key for lang_key in confirmed_keys if lang_key in present_keys],
        "missing_langs": [lang_by_key[lang_key]["lang"] for lang_key in missing_keys],
        "missing_lang_keys": missing_keys,
        "supplements": supplements,
    }


def validate_selected_groups(selected_group_ids, analysis_result):
    allowed_ids = {item["id"] for item in analysis_result["top_selected"]}
    allowed_ids.update(item["id"] for item in analysis_result["supplements"] if item.get("found"))
    invalid_ids = sorted({str(group_id) for group_id in selected_group_ids} - allowed_ids)
    if invalid_ids:
        raise ValueError(
            "发现不在 analyze_excel.py 输出中的广告组: "
            + ", ".join(invalid_ids)
            + "。最终名单只能使用 top_selected + supplements(found=true)。"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Excel 文件绝对路径")
    parser.add_argument("--top-langs", nargs="+", required=True, help="top 语言列表，支持中文、英文与语言代码混合输入")
    args = parser.parse_args()

    result = analyze_excel_file(args.file, args.top_langs)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
