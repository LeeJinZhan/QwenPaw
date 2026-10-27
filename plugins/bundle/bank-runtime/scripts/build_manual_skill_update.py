"""Export self-contained Skills for manual admin editing on existing readers.

Run from QwenPaw: python plugins/bundle/bank-runtime/scripts/build_manual_skill_update.py --output ../agentic-runtime/data/manual-skills-20261023
No installation, service mutation, or network access is performed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ('bank-file-delivery', 'bank-assistant-zh', 'bank-document-writing',
          'bank-document-review', 'bank-document-qa', 'bank-presentation')

GUIDE = """# 管理端手动更新试用说明

版本：2026-10-23；状态：本地候选，未在内网安装或验收。

本目录是适配现有 Skill 主文读取器的单文件兼容版，建议 DVL 先使用此版本。
manual-skill-update.zip是传输总包，先在个人电脑解压后阅读本说明，不把总包直接导入管理端。
格式契约由源码 references 自动合并到 bank-file-delivery 正文；没有遗漏格式参数，也不要求模型读取宿主文件。
源码拆分版依赖新增的 reference 读取代码，不能单独粘贴其简短主文到现有环境。
本次不更新镜像、补丁、身份配置、工具策略或数据库，不需要安装宿主 Python 或其它依赖。
Skills 只指导行为，不能替代工具启用、真实身份、权限或成果发布能力。

## 操作账号与范围

使用有目标 Agent 的 Skills 编辑权限的管理账号，进入 QwenPaw 管理端，选择实际绑定到业务助手的 Agent，再进入该 Agent 的 Skills 页面。
确认 Agent 标识和当前助手绑定；单个 Agent 的保存不代表其它 Agent 或全局生效。
bank-assistant-zh 的历史 frontmatter 名称为 bank_assistant，本包原样保留；以页面现有项和原内容核对，不因目录名差异重命名或创建重复项。
更新前暂停在该 Agent 上发起试用请求，保留现有业务会话供后续续聊验证。

## 备份、保存与检查

1. 逐个打开下列六个同名 Skill，复制保存前的完整 Markdown 到本地备份文件；记录 Agent、Skill 名、启用状态、渠道、原配置、备份位置和操作时间。若有现场定制，先对比并保留有效定制，不直接覆盖个人方法、业务事实或权限约束。
2. 先编辑 bank-file-delivery，粘贴本目录 bank-file-delivery.md 的完整内容，包括开头的 YAML 元信息，保持名称不变后保存。不要把“按需读取 reference”的源码主文粘贴到旧环境。
3. 依次编辑 bank-assistant-zh、bank-document-writing、bank-document-review、bank-document-qa、bank-presentation；使用同名 .md 的完整内容。保存前核对现场定制的合并结果。本次不调整 bank-chart 和分类技能。
4. 每项保存后重新打开，核对内容、名称、启用状态、渠道和原配置。若失败或出现冲突，保留备份并停止该项；不要为解决冲突创建带后缀的重复 Skill。页面保存会安排 Agent 重载，待页面恢复可用后用新会话验证。
5. .zip 是主文的导入备选，只在目标 Agent 确实缺少该 Skill 时使用；导入后核对名称和启用状态。已有同名项优先编辑保存，ZIP 导入不会自动覆盖，不能把整个交付目录作为一个 Skill 导入。bank-assistant-zh.zip 的历史元信息会被导入器识别为 bank_assistant，因此该项只建议编辑已有项，不直接导入，避免产生另一名称。
6. 保存现场最终合并稿并计算或登记摘要；清单 manifest.json 的摘要只对应本目录原文件，不能冒充现场合并稿摘要。在 80-42 登记 Agent 范围、来源、备份、最终摘要和结果；未知项填待补。

## 验收

分别在 deepseek-v4-flash 和 qwen3-27b 上验证新会话和历史续聊。使用实际业务材料及多种自然表达，不以固定句式命中作为成功标准。

| 任务 | 核对内容 |
| --- | --- |
| 普通问答、写正文、分析材料 | 按请求回答，不擅自新增文件或格式目标 |
| 已确认正文继续交付文件 | 沿用有效上下文，不重复问工具参数；核对内容完整 |
| 普通 Word、公文 Word | 文种与版式正确，主送和署名不编造，打开文件核对内容 |
| 表格、静态页面、文本或 SQL 文件 | 使用支持的受控工具；不是只有代码块、路径或 Shell 成功信息 |
| 修改已生成文件、转换上传材料 | 来源编号类型正确；完整保留未修改内容；读取转换不冒充下载成果 |
| 多成果或部分失败 | 每个要求分别核对，不将一项成功当作全部完成 |
| 参数错误、无权限、流式中断或取消 | 按诊断恢复或明确未完成，不绕过权限，不重复创建未知结果 |

文件成功至少核对工具结果、当前文件卡片、实际下载/打开内容。安装成功、模型请求无报错、行为正确、文件可下载是不同检查，不能相互替代。
记录模型、Agent、会话、原始请求、工具调用结果和成果状态。若仍只有 native_shell，保留本轮工具列表、Skill 读取和终态证据，再定位链路；本说明不宣称 Skills 已根治现场问题。

## 回退

使用同一管理账号在同一 Agent 中，把修改过的每个 Skill 恢复为备份的完整内容，并核对原启用状态、渠道和配置；重新打开确认保存成功，待重载后验证普通问答及原有文件交付。
只恢复本次修改范围，不删除会话或已交付文件，不回退镜像或其它补丁。恢复失败时停止继续覆盖，记录页面错误及备份位置交由管理员处理。
本次本地导出没有修改任何内网 Agent，实际更新时间、范围、合并稿摘要和备份位置均待现场登记。
"""


def compatible_content(name):
    source = ROOT / 'skills' / name
    text = (source / 'SKILL.md').read_text(encoding='utf-8')
    if name != 'bank-file-delivery':
        return text
    main, references = text.split('## 按需读取格式契约', 1)
    paths = re.findall(r'\]\((references/[A-Za-z0-9_-]+\.md)\)', references)
    if len(paths) != 4 or len(set(paths)) != len(paths):
        raise ValueError('Expected exactly four distinct format contracts')
    return main + '## 格式契约\n\n按当前所需格式使用以下契约；无关格式不增加交付目标。\n\n' + '\n\n'.join(
        (source / path).read_text(encoding='utf-8').strip() for path in paths) + '\n'


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in SKILLS:
        content = compatible_content(name).encode('utf-8')
        (output / f'{name}.md').write_bytes(content)
        with zipfile.ZipFile(output / f'{name}.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo(f'{name}/SKILL.md', date_time=(2026, 10, 23, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
        for suffix in ('md', 'zip'):
            filename = f'{name}.{suffix}'
            files[filename] = hashlib.sha256((output / filename).read_bytes()).hexdigest()
    (output / 'README.md').write_text(GUIDE, encoding='utf-8')
    files['README.md'] = hashlib.sha256((output / 'README.md').read_bytes()).hexdigest()
    manifest = {'version': '2026-10-23', 'mode': 'admin-compatible-main-only',
                'installed': False, 'files': files}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with zipfile.ZipFile(output / 'manual-skill-update.zip', 'w') as archive:
        for filename in sorted([*files, 'manifest.json']):
            info = zipfile.ZipInfo(f'manual-skills/{filename}', date_time=(2026, 10, 23, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (output / filename).read_bytes())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)
