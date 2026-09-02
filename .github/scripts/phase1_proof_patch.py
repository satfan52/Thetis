from pathlib import Path

p = Path(r"source/Project Files/Source/Console/audio.cs")
raw = p.read_bytes()
bom = raw.startswith(b"\xef\xbb\xbf")
use_crlf = b"\r\n" in raw
text = raw.decode("utf-8-sig").replace("\r\n", "\n")

def replace_once(old, new, label):
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 match, got {count}")
    text = text.replace(old, new, 1)

replace_once(
    "                ivac.SetIVACmonVol(0, monitor_volume);\n"
    "                cmaster.SetTCIRxAudioMonVol(0, monitor_volume);\n",
    "                ivac.SetIVACmonVol(0, monitor_volume);\n"
    "                ivac.SetIVACmonVol(1, monitor_volume);\n"
    "                cmaster.SetTCIRxAudioMonVol(0, monitor_volume);\n",
    "VAC2 monitor gain",
)

a = text.index("        public static bool MOX")
b = text.index("        private static void setupIVACforMon()", a)
segment = text[a:b]
target = "ivac.SetIVACmox(1, 0);"
if segment.count(target) != 2:
    raise RuntimeError(f"MOX policy: expected 2 VAC2 off assignments, got {segment.count(target)}")
segment = segment.replace(target, "ivac.SetIVACmox(1, vac2_enabled ? 1 : 0);")
text = text[:a] + segment + text[b:]

a = text.index("        private static void setupIVACforMon()")
b = text.index("        private static bool mon;", a)
segment = text[a:b]
target = "ivac.SetIVACmon(1, 0);"
if segment.count(target) != 2:
    raise RuntimeError(f"MON policy: expected 2 VAC2 off assignments, got {segment.count(target)}")
segment = segment.replace(target, "ivac.SetIVACmon(1, vac2_enabled ? 1 : 0);")
text = text[:a] + segment + text[b:]

old = """        public static bool VAC2Enabled
        {
            set
            {
                vac2_enabled = value;
                cmaster.CMSetAntiVoxSourceWhat();
                if (console.PowerOn)
                    EnableVAC2(value);
            }
            get { return vac2_enabled; }
        }
"""
new = """        public static bool VAC2Enabled
        {
            set
            {
                vac2_enabled = value;
                cmaster.CMSetAntiVoxSourceWhat();
                if (console.PowerOn)
                    EnableVAC2(value);

                // Phase 1 proof build: enabling VAC2 dedicates its output to post-DSP TX audio.
                // RX audio is suppressed on VAC2 so an external transmitter never receives RX AF.
                // The console MON control continues to govern VAC1 operator monitoring only.
                ivac.SetIVACmonVol(1, monitor_volume);
                setupIVACforMon();
                if (vac2_enabled || (mox && rx2_enabled && vfob_tx))
                    ivac.SetIVACmox(1, 1);
                else
                    ivac.SetIVACmox(1, 0);
            }
            get { return vac2_enabled; }
        }
"""
replace_once(old, new, "VAC2 enable proof policy")

encoded = text.replace("\n", "\r\n" if use_crlf else "\n").encode("utf-8")
if bom:
    encoded = b"\xef\xbb\xbf" + encoded
p.write_bytes(encoded)
print(f"Patched {p}")
