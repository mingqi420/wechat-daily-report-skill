#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成《wx-cli macOS 微信数据库密钥获取指南（开发者工具授权方式）》Word 文档。"""

from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import os

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "docs", "wx-cli-macos-key-extraction-guide.docx")
OUT_PATH = os.path.abspath(OUT_PATH)

doc = Document()

# ---------- 全局样式 ----------
style = doc.styles["Normal"]
style.font.name = "PingFang SC"
style.font.size = Pt(11)
style._element.rPr.rFonts.set(qn("w:eastAsia"), "PingFang SC")


def set_heading_font(paragraph, size, color=(0x1F, 0x4E, 0x79)):
    for run in paragraph.runs:
        run.font.name = "PingFang SC"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "PingFang SC")
        run.font.size = Pt(size)
        run.font.color.rgb = RGBColor(*color)
        run.font.bold = True


def add_code_block(text):
    """等宽字体代码块，带浅灰底纹。"""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(8)
    p.paragraph_format.line_spacing = 1.15
    # 底纹
    pPr = p._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), "F2F2F2")
    pPr.append(shd)
    for i, line in enumerate(text.strip().split("\n")):
        if i > 0:
            p.add_run().add_break()
        run = p.add_run(line)
        run.font.name = "Menlo"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "Menlo")
        run.font.size = Pt(9.5)
    return p


def add_bullet(text, bold_prefix=None):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(2)
    if bold_prefix:
        r = p.add_run(bold_prefix)
        r.bold = True
    p.add_run(text)
    return p


# ---------- 标题 ----------
title = doc.add_heading("wx-cli macOS 微信数据库密钥获取指南", level=0)
for run in title.runs:
    run.font.name = "PingFang SC"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "PingFang SC")
    run.font.size = Pt(22)
    run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

sub = doc.add_paragraph("（开发者工具授权方式 / GUI Terminal + Developer Tools）")
sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
for run in sub.runs:
    run.font.name = "PingFang SC"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "PingFang SC")
    run.font.size = Pt(12)
    run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

meta = doc.add_paragraph()
meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = meta.add_run("适用范围：macOS 13+ / 微信 4.x / wx-cli 0.3.0    ·    更新日期：2026-08-31")
r.font.size = Pt(10)
r.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

doc.add_paragraph()

# ---------- 一、问题现象 ----------
h = doc.add_heading("一、问题现象", level=1)
set_heading_font(h, 16)

doc.add_paragraph("运行 wx-cli 的会话命令时，报错「无法解密 session.db」：")
add_code_block("""$ wx sessions
启动 wx-daemon...
错误: 无法解密 session.db""")
doc.add_paragraph("该报错由 wx-cli 内置的 wx-daemon 抛出，表明其无法读取到微信数据库的加密密钥，因此无法解密本地 SQLite 数据库。")

# ---------- 二、根因分析 ----------
h = doc.add_heading("二、根因分析", level=1)
set_heading_font(h, 16)

doc.add_paragraph("经过逐项排查，定位到如下链路：")
add_bullet("，检查发现文件内容为空（{}，密钥数量为 0），这是无法解密的直接原因。", bold_prefix="1. 密钥文件缺失：")
add_bullet("（wx init）负责从微信进程内存中扫描 SQLite 加密密钥，扫描失败则密钥文件保持为空。", bold_prefix="2. 密钥扫描失败：")
add_bullet("在微信 4.x 中，进程内存中不存在明文密钥，需要从运行中的微信进程读取内存；macOS 的 TCC（隐私保护）默认拦截终端对微信进程的跨进程内存读取（task_for_pid），导致扫描拿到 0 个候选密钥。", bold_prefix="3. 权限拦截：")
add_bullet("微信 4.1.13 已开启 Hardened Runtime，系统签名下对进程调试权限的校验更严格。", bold_prefix="4. 版本因素：")

doc.add_paragraph("诊断命令（用于复现与确认）：")
add_code_block("""# 确认密钥文件内容
cat ~/.wx-cli/all_keys.json

# 统计密钥数量
python3 -c "import json;print('密钥数量:', len(json.load(open('$HOME/.wx-cli/all_keys.json'))))"

# 确认微信进程与版本
pgrep -x WeChat && defaults read /Applications/WeChat.app/Contents/Info.plist CFBundleShortVersionString""")

# ---------- 三、解决方案 ----------
h = doc.add_heading("三、解决方案：开发者工具授权方式", level=1)
set_heading_font(h, 16)

doc.add_paragraph("核心思路：通过 macOS「开发者工具」授权，让终端（Terminal / iTerm2）获得对微信进程执行 task_for_pid（读取进程内存）的权限，从而使 wx init 能从内存中扫描到数据库密钥。该方式适用于微信保持 Apple 官方签名（Hardened Runtime）的场景，无需对微信进行重签名。")

h2 = doc.add_heading("3.1 原理说明", level=2)
set_heading_font(h2, 14)
add_bullet("「开发者工具」授权决定是否允许对带 Hardened Runtime 的 App 执行 task_for_pid（读取进程内存）。", bold_prefix="开发者工具（Developer Tools）：")
add_bullet("控制能否读写受保护目录/文件，与进程调试无关，此场景无需配置。", bold_prefix="完全磁盘访问（Full Disk Access）：")
add_bullet("当目标 App 带 Hardened Runtime 时，taskgated 会检查调用者（responsible app）是否具有 TCC DeveloperTool 授权；GUI 终端（负责应用为本机 Terminal.app）可以被授权，而 SSH（负责应用为 sshd）无法获得该授权。", bold_prefix="校验机制：")

h2 = doc.add_heading("3.2 操作步骤", level=2)
set_heading_font(h2, 14)

doc.add_paragraph("步骤一：授予终端「开发者工具」权限", style="List Number")
doc.add_paragraph("打开「系统设置 → 隐私与安全性 → 开发者工具」，在列表中为当前使用的终端 App（Terminal.app 或 iTerm2）打开开关。若列表中没有，点击「+」添加，使用 Cmd+Shift+G 输入：")
add_code_block("/System/Applications/Utilities/Terminal.app")
doc.add_paragraph("也可以跳过手动设置：直接在终端中运行下面的验证命令，系统会弹窗提示「Terminal 想要进行开发工具访问」，点击「允许」即可自动完成授权。")

doc.add_paragraph("步骤二：完全退出终端并重新打开", style="List Number")
doc.add_paragraph("开发者工具授权只在进程启动时检查，不会热更新。必须完全退出终端（Cmd+Q）再重新打开，否则授权不会生效。")

doc.add_paragraph("步骤三：验证授权是否生效", style="List Number")
doc.add_paragraph("用 lldb 尝试附加到微信进程，能进入 (lldb) 交互界面即代表授权成功：")
add_code_block("""# 获取微信 PID
pgrep -x WeChat

# 附加调试（首次运行会弹窗授权，点击允许）
sudo lldb -p <PID>

# 看到如下输出即授权成功：
# Process 6313 stopped
# Target 0: (WeChat) stopped.
# (lldb)

# 在 (lldb) 提示符下输入 q 退出并 detach（输入 y 确认），让微信继续运行""")

doc.add_paragraph("步骤四：重新扫描密钥", style="List Number")
add_code_block("# 强制重新初始化，扫描微信内存中的数据库密钥\n"
               "sudo wx init --force\n"
               "\n"
               "# 确认密钥数量大于 0\n"
               "python3 -c \"import json;print('密钥数量:', len(json.load(open('$HOME/.wx-cli/all_keys.json'))))\"")
doc.add_paragraph("密钥数量大于 0 即代表扫描成功。")

doc.add_paragraph("步骤五：重新运行会话命令", style="List Number")
add_code_block("""wx sessions""")
doc.add_paragraph("此时应能正常列出微信会话，不再报「无法解密 session.db」。")

# ---------- 四、故障排查 ----------
h = doc.add_heading("四、常见问题排查", level=1)
set_heading_font(h, 16)

doc.add_paragraph("1. lldb 附加时报 KERN_FAILURE / task_for_pid 失败")
doc.add_paragraph("说明开发者工具授权未生效。检查：是否在正确的终端（GUI 而非 SSH）中执行；授权开关是否打开；是否完全退出并重开终端。", style="List Bullet")

doc.add_paragraph("2. 授权后 wx init 仍显示 0 个密钥")
doc.add_paragraph("权限问题已排除，大概率是 wx-cli 版本与微信版本不兼容（如 wx-cli 0.3.0 尚未适配微信 4.1.13 的密钥格式/位置）。可尝试：等待 wx-cli 上游更新；降级微信至 4.0.x / 4.1 早期版本；改用备用方案（见第五章）。", style="List Bullet")

doc.add_paragraph("3. SSH 远程场景不可用")
doc.add_paragraph("SSH 的负责应用是 sshd，无法获得开发者工具授权，task_for_pid 必然失败。远程场景需改用「完全磁盘访问 + ad-hoc 重签名微信」方案。", style="List Bullet")

doc.add_paragraph("4. 重签名微信后，TCC 授权记录失效")
doc.add_paragraph("重签名后微信的代码签名要求（csreq）变化，旧授权会失效（表现为开关开着但功能被拒）。可重置微信相关 TCC 记录后重新授权：", style="List Bullet")
add_code_block("""for s in ScreenCapture Camera Microphone AppleEvents AddressBook SystemPolicyDocumentsFolder SystemPolicyDownloadsFolder SystemPolicyDesktopFolder; do
  tccutil reset "$s" com.tencent.xinWeChat
done
killall WeChat && open /Applications/WeChat.app""")

doc.add_paragraph("5. 微信自动更新后再次报错")
doc.add_paragraph("微信自动更新会覆盖签名/变更版本，需要重新走一遍授权或重签流程。", style="List Bullet")

# ---------- 五、备用方案 ----------
h = doc.add_heading("五、备用方案（SSH 场景 / 无法 GUI 授权时）", level=1)
set_heading_font(h, 16)

doc.add_paragraph("若无法使用 GUI 终端授权（如纯 SSH 远程操作），可退而求其次，将微信临时重签为 ad-hoc 签名（去掉 Hardened Runtime）后扫描：")
add_code_block("""# 1. 完全退出微信（运行中的 binary 被占用，重签会失败）
osascript -e 'quit app "WeChat"'
pgrep -x WeChat || echo "微信已退出"

# 2. ad-hoc 重签名
sudo codesign --force --deep --sign - /Applications/WeChat.app

# 3. 验证签名（期望 flags=0x2(adhoc)）
codesign -dv /Applications/WeChat.app 2>&1 | grep -E "Signature|flags"

# 4. 重新打开微信并登录
open /Applications/WeChat.app

# 5. 重新扫描密钥
sudo wx init --force""")
doc.add_paragraph("注意：此方案会破坏微信签名完整性，可能出现「想访问其他 App 的数据」弹窗、部分小程序报错等问题，且微信更新后需重复操作。")

# ---------- 六、安全提示 ----------
h = doc.add_heading("六、安全提示", level=1)
set_heading_font(h, 16)
add_bullet("从微信进程内存提取密钥、修改 TCC 授权属于敏感操作，仅在个人可控环境中进行。")
add_bullet("开发者工具授权会扩大终端进程的访问范围，注意使用完毕后可在系统设置中关闭开关。")
add_bullet("tccutil reset 会清除微信现有授权记录，操作前请确认影响。")
add_bullet("提取出的密钥与解密后的数据库文件请妥善保管，避免泄露个人聊天数据。")

# ---------- 七、命令速查 ----------
h = doc.add_heading("七、命令速查", level=1)
set_heading_font(h, 16)
add_code_block("""# 检查密钥
cat ~/.wx-cli/all_keys.json

# 开发者工具授权验证
pgrep -x WeChat
sudo lldb -p <PID>          # 能进入 (lldb) 即授权成功，q 退出

# 扫描密钥
sudo wx init --force

# 使用
wx sessions""")

# ---------- 保存 ----------
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
doc.save(OUT_PATH)
print("文档已生成:", OUT_PATH)
