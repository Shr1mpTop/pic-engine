# -*- coding: utf-8 -*-
"""{{占位符}} 替换 —— 语义复刻自 outfit_studio/__init__.py 的 OutfitTemplateApply。

唯一允许的"复刻"：这是纯字符串操作（30 行），不值得为它拖着 outfit_studio。
两处保持一字不差：raw_decode 扫描（容忍 JSON 外有杂质）与中文别名表。
"""
import json
import re

# 旧中文占位符 -> 新 schema 字段名，两套写法都能替换（原表原样）
ALIASES = {
    "身材": "body_type", "体质": "physique", "肩": "shoulders",
    "腰": "waist", "臀": "hips", "腿": "legs", "胸": "chest",
    "肤色": "skin_tone", "腋毛": "axillary_hair",
    "体毛": "limb_hair", "私处": "pussy",
}


def extract_values(analysis):
    """analysis（dict 或 JSON 字符串）-> {字段: 值}。找不到合法 JSON 就返回空表。"""
    if isinstance(analysis, dict):
        return {k: v for k, v in analysis.items() if isinstance(v, (str, int, float))}
    text = str(analysis or "")
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, (str, int, float))}
    return {}


def apply(analysis, template):
    """模板里 {{字段名}} 被分析结果同名字段替换；缺字段占位符原样保留（可发现漏填）。"""
    values = extract_values(analysis)
    text = str(template)
    for key, value in values.items():
        text = text.replace("{{%s}}" % key, str(value))
    for cn, en in ALIASES.items():
        if en in values:
            text = text.replace("{{%s}}" % cn, str(values[en]))
    return text
