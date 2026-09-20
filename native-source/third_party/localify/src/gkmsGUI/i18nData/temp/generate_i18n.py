#!/usr/bin/env python3
import os
import argparse
import xml.etree.ElementTree as ET

def parse_strings_xml(file_path):
    """
    解析 Android strings.xml 文件，返回 (key, value) 列表
    """
    pairs = []
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        # 遍历所有 <string> 元素
        for string_elem in root.findall('string'):
            key = string_elem.get('name')
            if key is None:
                continue
            value = string_elem.text or ""
            # 处理转义字符：将 \ 转为 \\ ，" 转为 \"
            value = value.replace('\\', '\\\\').replace('"', '\\"')
            pairs.append((key, value))
    except Exception as e:
        print(f"解析 {file_path} 失败: {e}")
    return pairs

def get_var_name(folder_name):
    """
    根据文件夹名称生成对应的 C++ 变量名
    """
    if folder_name == "values":
        return "i18nData_default"
    elif folder_name.startswith("values-"):
        suffix = folder_name[len("values-"):]
        suffix = suffix.replace('-', '_')
        return "i18nData_" + suffix
    else:
        return "i18nData"

def generate_header(pairs, output_filename, var_name):
    """
    根据 key/value 对生成 C++ 头文件代码，并写入 output_filename 文件中，
    变量名使用传入的 var_name
    """
    header_lines = [
        "#pragma once",
        "",
        "#include <unordered_map>",
        "#include <string>",
        "",
        "namespace I18nData {",
        f"    static const std::unordered_map<std::string, std::string> {var_name} = {{"
    ]
    # 为每个 key/value 对生成一行代码
    for key, value in pairs:
        header_lines.append(f'        {{ "{key}", "{value}" }},')
    header_lines.append("    };")
    header_lines.append("}")
    # 将生成的内容写入文件
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(header_lines))
    print(f"生成 {output_filename} 成功，变量名为 {var_name}.")

def main():
    parser = argparse.ArgumentParser(description="根据安卓 strings.xml 文件生成 C++ unordered_map 代码")
    parser.add_argument("folder", help="根目录文件夹路径，该文件夹下应包含 app/src/main/res")
    args = parser.parse_args()

    # 定义需要转换的文件夹和对应的输出 .hpp 文件名
    files = {
        "values-zh-rCN": "strings_zh-rCN.hpp",
        "values-ja": "strings_ja.hpp",
        "values": "strings_en.hpp"
    }
    base_path = os.path.join(args.folder, "app", "src", "main", "res")
    for folder_name, output_file in files.items():
        xml_path = os.path.join(base_path, folder_name, "strings.xml")
        if not os.path.isfile(xml_path):
            print(f"文件 {xml_path} 不存在，跳过.")
            continue
        print(f"解析文件：{xml_path}")
        pairs = parse_strings_xml(xml_path)
        var_name = get_var_name(folder_name)
        generate_header(pairs, output_file, var_name)

if __name__ == "__main__":
    main()
