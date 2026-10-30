#!/usr/bin/python3
"""Add explicit Unicode typing button to the installed noVNC UI; no desktop restart."""
from pathlib import Path
import os
assert os.geteuid()==0, "Run with sudo in Tencent console"
base=Path('/usr/share/novnc')
html=base/'vnc.html'; js=base/'app/ui.js'
h=html.read_text();s=js.read_text()
marker='noVNC_unicode_send_button'
handler="""        document.getElementById("noVNC_unicode_send_button")
            .addEventListener('click', () => {
                const text = document.getElementById('noVNC_clipboard_text').value;
                if (!UI.rfb || !text || Array.from(text).length > 1000) return;
                [0xffe3,0xffe4,0xffe1,0xffe2,0xffe9,0xffea].forEach(k => UI.rfb.sendKey(k, null, false));
                UI.closeClipboardPanel();
                UI.rfb.focus();
                let delay = 0;
                for (const char of text) {
                    const cp = char.codePointAt(0);
                    const key = cp === 10 ? 0xff0d : cp < 128 ? cp : (0x01000000 | cp);
                    setTimeout(() => { UI.rfb.sendKey(key, null, true); UI.rfb.sendKey(key, null, false); }, delay);
                    delay += 20;
                }
            });
"""
if marker not in h:
 assert h.count('<textarea id="noVNC_clipboard_text" rows=5></textarea>')==1
 h=h.replace('<textarea id="noVNC_clipboard_text" rows=5></textarea>', '<textarea id="noVNC_clipboard_text" rows=5></textarea>\n<p>先点击远程输入框，再在这里填写文字，点击下方按钮；无需 Ctrl+V。</p>\n<input id="noVNC_unicode_send_button" type="button" value="输入中文到远程窗口" class="noVNC_submit">')
if marker not in s:
 needle='    addClipboardHandlers() {'
 assert s.count(needle)==1
 s=s.replace(needle,needle+'\n'+handler)
 needle='        UI.rfb.clipboardPasteFrom(text);'
 assert s.count(needle)==1
 s=s.replace(needle,"        if (!Array.from(text).some(c => c.codePointAt(0) > 255)) UI.rfb.clipboardPasteFrom(text);")
for p,text in ((html,h),(js,s)):
 backup=p.with_name(p.name+'.pre-unicode')
 if not backup.exists():backup.write_bytes(p.read_bytes())
 p.write_text(text)
print('Unicode input button installed. Refresh noVNC browser tab; server-side Douyin login was not restarted.')
