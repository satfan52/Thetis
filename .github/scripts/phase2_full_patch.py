from pathlib import Path
import runpy

ROOT = Path("source")

# Start from the compile-validated native output-only IVAC patch.
runpy.run_path(str(Path(".github/scripts/phase2_native_patch.py")), run_name="__main__")


def load(rel):
    p = ROOT / rel
    raw = p.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    return p, text, bom, crlf


def save(p, text, bom, crlf):
    data = text.replace("\n", "\r\n" if crlf else "\n").encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    p.write_bytes(data)


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f"{label}: expected 1 match, got {n}")
    return text.replace(old, new, 1)


# ---- Console/ivac.cs: expose native output-only mode ----------------------
p, text, bom, crlf = load("Project Files/Source/Console/ivac.cs")
anchor = '''        [DllImport("ChannelMaster.dll", EntryPoint = "SetIVACcombine", CallingConvention = CallingConvention.Cdecl)]
        public static extern void SetIVACcombine(int id, int combine);

'''
addition = '''        [DllImport("ChannelMaster.dll", EntryPoint = "SetIVACOutputOnly", CallingConvention = CallingConvention.Cdecl)]
        public static extern void SetIVACOutputOnly(int id, int output_only);

'''
text = once(text, anchor, anchor + addition, "ivac.cs SetIVACOutputOnly import")
save(p, text, bom, crlf)


# ---- Console/audio.cs: reversible mode + independent settings ------------
p, text, bom, crlf = load("Project Files/Source/Console/audio.cs")

anchor = '''        private static int output_dev3 = 0;
        public static int Output3
        {
            get { return output_dev3; }
            set { output_dev3 = value; }
        }

'''
addition = r'''        // VAC2 can remain a completely normal duplex VAC (default), or be
        // temporarily repurposed as a dedicated post-TX-DSP audio output.
        // The processed-output device settings are intentionally independent
        // so switching modes never overwrites the user's normal VAC2 setup.
        private static bool vac2_processed_tx_output = false;
        private static int vac2_processed_host = 0;
        private static int vac2_processed_output = 0;
        private static int vac2_processed_sample_rate = 48000;
        private static int vac2_processed_block_size = 512;
        private static int vac2_processed_exclusive_out = 0;
        private static double vac2_processed_gain_db = 0.0;

        public static bool VAC2ProcessedTXOutput
        {
            get { return vac2_processed_tx_output; }
            set
            {
                if (vac2_processed_tx_output == value) return;
                bool restart = vac2_enabled && console != null && console.PowerOn;
                if (restart) EnableVAC2(false);
                vac2_processed_tx_output = value;
                if (restart) EnableVAC2(true);
            }
        }

        public static int VAC2ProcessedHost
        {
            get { return vac2_processed_host; }
            set { vac2_processed_host = value; }
        }

        public static int VAC2ProcessedOutput
        {
            get { return vac2_processed_output; }
            set { vac2_processed_output = value; }
        }

        public static int VAC2ProcessedSampleRate
        {
            get { return vac2_processed_sample_rate; }
            set { vac2_processed_sample_rate = value; }
        }

        public static int VAC2ProcessedBlockSize
        {
            get { return vac2_processed_block_size; }
            set { vac2_processed_block_size = value; }
        }

        public static int VAC2ProcessedExclusiveOut
        {
            get { return vac2_processed_exclusive_out; }
            set { vac2_processed_exclusive_out = value; }
        }

        public static double VAC2ProcessedGainDB
        {
            get { return vac2_processed_gain_db; }
            set
            {
                vac2_processed_gain_db = value;
                if (vac2_processed_tx_output)
                    ivac.SetIVACmonVol(1, Math.Pow(10.0, vac2_processed_gain_db / 20.0));
            }
        }

        public static void RestartVAC2()
        {
            if (!vac2_enabled || console == null || !console.PowerOn) return;
            EnableVAC2(false);
            EnableVAC2(true);
        }

'''
text = once(text, anchor, anchor + addition, "audio.cs processed VAC2 settings")

start = text.index("        public static void EnableVAC2(bool enable)\n        {")
end = text.index("\n        private static RadioProtocol _lastRadioProtocol", start)
new_enable = r'''        public static void EnableVAC2(bool enable)
        {
            bool retval = false;

            if (enable)
                unsafe
                {
                    if (vac2_processed_tx_output)
                    {
                        // Dedicated external-transmitter path.  No PortAudio input device
                        // is opened and native IVAC ignores RX audio as a mixer dependency.
                        int global_output = (int)PA19.PA_HostApiDeviceIndexToDeviceIndex(vac2_processed_host, vac2_processed_output);
                        PA19.PaDeviceInfo out_info = PA19.PA_GetDeviceInfo(global_output);
                        if (global_output < 0 || out_info.maxOutputChannels < 2)
                        {
                            MessageBox.Show("The selected Processed TX Output device is unavailable or does not expose two output channels.\n" +
                                "Please select another output device on Setup -> Audio -> VAC 2.",
                                "VAC2 Processed TX Output Error",
                                MessageBoxButtons.OK,
                                MessageBoxIcon.Error);
                            return;
                        }

                        double out_latency = out_info.defaultLowOutputLatency;

                        VAC2RBReset = true;
                        ivac.SetIVACOutputOnly(1, 1);
                        ivac.SetIVACiqType(1, 0);               // always audio, never Direct I/Q
                        ivac.SetIVACstereo(1, 0);               // source is processed TX speech
                        ivac.SetIVAChostAPIindex(1, vac2_processed_host);
                        ivac.SetIVACoutputDEVindex(1, vac2_processed_output);
                        ivac.SetIVACnumChannels(1, 2);
                        ivac.SetIVACvacRate(1, vac2_processed_sample_rate);
                        ivac.SetIVACvacSize(1, vac2_processed_block_size);
                        ivac.SetIVACOutLatency(1, out_latency, 0);
                        ivac.SetIVACPAOutLatency(1, out_latency, 1);
                        ivac.SetIVACExclusiveIn(1, 0);
                        ivac.SetIVACExclusiveOut(1, vac2_processed_exclusive_out);
                        ivac.SetIVACmonVol(1, Math.Pow(10.0, vac2_processed_gain_db / 20.0));

                        try
                        {
                            retval = ivac.StartAudioIVAC(1) == 1;
                            if (retval && console.PowerOn)
                                ivac.SetIVACrun(1, 1);
                        }
                        catch (Exception)
                        {
                            MessageBox.Show("The program is having trouble starting the Processed TX Output stream.\n" +
                                "Please examine the VAC2 Processed TX Output settings and try again.",
                                "VAC2 Processed TX Output Startup Error",
                                MessageBoxButtons.OK,
                                MessageBoxIcon.Error);
                        }
                    }
                    else
                    {
                        // Exact legacy VAC2 path.  Restore every native mode flag that may
                        // have been changed by Processed TX Output before opening duplex VAC2.
                        ivac.SetIVACOutputOnly(1, 0);
                        ivac.SetIVACiqType(1, Convert.ToInt32(vac2_output_iq));
                        ivac.SetIVACstereo(1, Convert.ToInt32(vac2_stereo));
                        ivac.SetIVACExclusiveIn(1, _exclusive_in_vac2);
                        ivac.SetIVACExclusiveOut(1, _exclusive_out_vac2);

                        int num_chan = 1;
                        int sample_rate = sample_rate3;
                        int block_size = block_size_vac2;

                        double in_latency = vac2_latency_manual ? latency3 / 1000.0 : PA19.PA_GetDeviceInfo(input_dev3).defaultLowInputLatency;
                        double out_latency = vac2_latency_out_manual ? vac2_latency_out / 1000.0 : PA19.PA_GetDeviceInfo(output_dev3).defaultLowOutputLatency;
                        double pa_in_latency = vac2_latency_pa_in_manual ? vac2_latency_pa_in / 1000.0 : PA19.PA_GetDeviceInfo(input_dev3).defaultLowInputLatency;
                        double pa_out_latency = vac2_latency_pa_out_manual ? vac2_latency_pa_out / 1000.0 : PA19.PA_GetDeviceInfo(output_dev3).defaultLowOutputLatency;

                        if (vac2_output_iq)
                        {
                            num_chan = 2;
                            sample_rate = sample_rate_rx2;
                            block_size = block_size_rx2;
                        }
                        else if (vac2_stereo) num_chan = 2;

                        VAC2RBReset = true;

                        ivac.SetIVAChostAPIindex(1, host3);
                        ivac.SetIVACinputDEVindex(1, input_dev3);
                        ivac.SetIVACoutputDEVindex(1, output_dev3);
                        ivac.SetIVACnumChannels(1, num_chan);
                        ivac.SetIVACvacRate(1, sample_rate);
                        ivac.SetIVACvacSize(1, block_size);
                        ivac.SetIVACInLatency(1, in_latency, 0);
                        ivac.SetIVACOutLatency(1, out_latency, 0);
                        ivac.SetIVACPAInLatency(1, pa_in_latency, 0);
                        ivac.SetIVACPAOutLatency(1, pa_out_latency, 1);

                        // MW0LGE_21h
                        ivac.SetIVACFeedbackGain(1, 0, vac2_feedbackgainOut);
                        ivac.SetIVACFeedbackGain(1, 1, vac2_feedbackgainIn);
                        ivac.SetIVACSlewTime(1, 0, vac2_slewtimeOut);
                        ivac.SetIVACSlewTime(1, 1, vac2_slewtimeIn);
                        //

                        // MW0LGE_21j
                        ivac.SetIVACPropRingMin(1, 0, vac2_prop_ringminOut);
                        ivac.SetIVACPropRingMin(1, 1, vac2_prop_ringminIn);
                        ivac.SetIVACPropRingMax(1, 0, vac2_prop_ringmaxOut);
                        ivac.SetIVACPropRingMax(1, 1, vac2_prop_ringmaxIn);
                        ivac.SetIVACFFRingMin(1, 0, vac2_ff_ringminOut);
                        ivac.SetIVACFFRingMin(1, 1, vac2_ff_ringminIn);
                        ivac.SetIVACFFRingMax(1, 0, vac2_ff_ringmaxOut);
                        ivac.SetIVACFFRingMax(1, 1, vac2_ff_ringmaxIn);
                        ivac.SetIVACFFAlpha(1, 0, vac2_ff_alphaOut);
                        ivac.SetIVACFFAlpha(1, 1, vac2_ff_alphaIn);
                        ivac.SetIVACswapIQout(1, _swap_iq_vac2);
                        ivac.SetIVACinitialVars(1, vac2_oldVarIn, vac2_oldVarOut);
                        //

                        try
                        {
                            retval = ivac.StartAudioIVAC(1) == 1;
                            if (retval && console.PowerOn)
                                ivac.SetIVACrun(1, 1);
                        }
                        catch (Exception)
                        {
                            MessageBox.Show("The program is having trouble starting the VAC audio streams.\n" +
                                "Please examine the VAC related settings on the Setup Form -> Audio Tab and try again.",
                                "VAC2 Audio Stream Startup Error",
                                MessageBoxButtons.OK,
                                MessageBoxIcon.Error);
                        }
                    }
                }
            else
            {
                ivac.SetIVACrun(1, 0);
                ivac.StopAudioIVAC(1);
            }
            Thread.Sleep(10); // prevent ASIO exception
        }
'''
text = text[:start] + new_enable + text[end:]
save(p, text, bom, crlf)


# ---- Console/setup.cs: runtime GUI, independently persisted controls -------
p, text, bom, crlf = load("Project Files/Source/Console/setup.cs")

field_anchor = "        #region Constructor and Destructor\n"
fields = r'''        #region VAC2 Processed TX Output Controls

        private System.Windows.Forms.LabelTS lblVAC2OperatingMode;
        private System.Windows.Forms.ComboBoxTS comboVAC2OperatingMode;
        private System.Windows.Forms.LabelTS lblVAC2ProcessedModeNote;
        private System.Windows.Forms.LabelTS lblVAC2ProcessedDriver;
        private System.Windows.Forms.ComboBoxTS comboVAC2ProcessedDriver;
        private System.Windows.Forms.LabelTS lblVAC2ProcessedInput;
        private System.Windows.Forms.LabelTS lblVAC2ProcessedOutput;
        private System.Windows.Forms.ComboBoxTS comboVAC2ProcessedOutput;
        private System.Windows.Forms.ComboBoxTS comboVAC2ProcessedSampleRate;
        private System.Windows.Forms.ComboBoxTS comboVAC2ProcessedBuffer;
        private System.Windows.Forms.LabelTS lblVAC2ProcessedGain;
        private System.Windows.Forms.NumericUpDownTS udVAC2ProcessedTXGain;
        private System.Windows.Forms.CheckBoxTS chkVAC2ProcessedExclusiveOut;

        #endregion

'''
text = once(text, field_anchor, fields + field_anchor, "setup.cs runtime control fields")

text = once(
    text,
    "            console = c;\n            this.Owner = c;\n\n",
    "            console = c;\n            this.Owner = c;\n\n            InitializeVAC2ProcessedTXControls();\n\n",
    "setup.cs initialize Phase 2 controls",
)

method_anchor = "        internal void AfterConstructor()\n"
methods = r'''        private void InitializeVAC2ProcessedTXControls()
        {
            lblVAC2OperatingMode = new System.Windows.Forms.LabelTS();
            lblVAC2OperatingMode.Name = "lblVAC2OperatingMode";
            lblVAC2OperatingMode.Text = "Mode:";
            lblVAC2OperatingMode.Location = new System.Drawing.Point(160, 219);
            lblVAC2OperatingMode.Size = new System.Drawing.Size(40, 16);

            comboVAC2OperatingMode = new System.Windows.Forms.ComboBoxTS();
            comboVAC2OperatingMode.Name = "comboVAC2OperatingMode";
            comboVAC2OperatingMode.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            comboVAC2OperatingMode.Location = new System.Drawing.Point(202, 216);
            comboVAC2OperatingMode.Size = new System.Drawing.Size(210, 21);
            comboVAC2OperatingMode.Items.Add("Normal VAC");
            comboVAC2OperatingMode.Items.Add("Processed TX Output");
            comboVAC2OperatingMode.SelectedIndex = 0;

            lblVAC2ProcessedModeNote = new System.Windows.Forms.LabelTS();
            lblVAC2ProcessedModeNote.Name = "lblVAC2ProcessedModeNote";
            lblVAC2ProcessedModeNote.Text = "Post-TX-DSP audio; VAC2 input disabled";
            lblVAC2ProcessedModeNote.Location = new System.Drawing.Point(160, 242);
            lblVAC2ProcessedModeNote.Size = new System.Drawing.Size(250, 18);

            lblVAC2ProcessedDriver = new System.Windows.Forms.LabelTS();
            lblVAC2ProcessedDriver.Name = "lblVAC2ProcessedDriver";
            lblVAC2ProcessedDriver.Text = "Driver:";
            lblVAC2ProcessedDriver.Location = new System.Drawing.Point(8, 18);
            lblVAC2ProcessedDriver.Size = new System.Drawing.Size(40, 16);

            comboVAC2ProcessedDriver = new System.Windows.Forms.ComboBoxTS();
            comboVAC2ProcessedDriver.Name = "comboVAC2ProcessedDriver";
            comboVAC2ProcessedDriver.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            comboVAC2ProcessedDriver.Location = new System.Drawing.Point(56, 18);
            comboVAC2ProcessedDriver.Size = new System.Drawing.Size(160, 21);

            lblVAC2ProcessedInput = new System.Windows.Forms.LabelTS();
            lblVAC2ProcessedInput.Name = "lblVAC2ProcessedInput";
            lblVAC2ProcessedInput.Text = "Input: None (output-only)";
            lblVAC2ProcessedInput.Location = new System.Drawing.Point(8, 45);
            lblVAC2ProcessedInput.Size = new System.Drawing.Size(180, 16);

            lblVAC2ProcessedOutput = new System.Windows.Forms.LabelTS();
            lblVAC2ProcessedOutput.Name = "lblVAC2ProcessedOutput";
            lblVAC2ProcessedOutput.Text = "Output:";
            lblVAC2ProcessedOutput.Location = new System.Drawing.Point(8, 70);
            lblVAC2ProcessedOutput.Size = new System.Drawing.Size(48, 16);

            comboVAC2ProcessedOutput = new System.Windows.Forms.ComboBoxTS();
            comboVAC2ProcessedOutput.Name = "comboVAC2ProcessedOutput";
            comboVAC2ProcessedOutput.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            comboVAC2ProcessedOutput.Location = new System.Drawing.Point(56, 70);
            comboVAC2ProcessedOutput.Size = new System.Drawing.Size(270, 21);

            comboVAC2ProcessedSampleRate = new System.Windows.Forms.ComboBoxTS();
            comboVAC2ProcessedSampleRate.Name = "comboVAC2ProcessedSampleRate";
            comboVAC2ProcessedSampleRate.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            comboVAC2ProcessedSampleRate.Location = new System.Drawing.Point(16, 24);
            comboVAC2ProcessedSampleRate.Size = new System.Drawing.Size(64, 21);
            comboVAC2ProcessedSampleRate.Items.AddRange(new object[] { "44100", "48000", "96000", "192000" });
            comboVAC2ProcessedSampleRate.SelectedItem = "48000";

            comboVAC2ProcessedBuffer = new System.Windows.Forms.ComboBoxTS();
            comboVAC2ProcessedBuffer.Name = "comboVAC2ProcessedBuffer";
            comboVAC2ProcessedBuffer.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            comboVAC2ProcessedBuffer.Location = new System.Drawing.Point(17, 24);
            comboVAC2ProcessedBuffer.Size = new System.Drawing.Size(64, 21);
            comboVAC2ProcessedBuffer.Items.AddRange(new object[] { "64", "128", "256", "512", "1024", "2048" });
            comboVAC2ProcessedBuffer.SelectedItem = "512";

            lblVAC2ProcessedGain = new System.Windows.Forms.LabelTS();
            lblVAC2ProcessedGain.Name = "lblVAC2ProcessedGain";
            lblVAC2ProcessedGain.Text = "TX Out:";
            lblVAC2ProcessedGain.Location = new System.Drawing.Point(8, 28);
            lblVAC2ProcessedGain.Size = new System.Drawing.Size(42, 16);

            udVAC2ProcessedTXGain = new System.Windows.Forms.NumericUpDownTS();
            udVAC2ProcessedTXGain.Name = "udVAC2ProcessedTXGain";
            udVAC2ProcessedTXGain.DecimalPlaces = 1;
            udVAC2ProcessedTXGain.Increment = 0.5M;
            udVAC2ProcessedTXGain.Minimum = -30M;
            udVAC2ProcessedTXGain.Maximum = 20M;
            udVAC2ProcessedTXGain.Value = 0M;
            udVAC2ProcessedTXGain.Location = new System.Drawing.Point(50, 25);
            udVAC2ProcessedTXGain.Size = new System.Drawing.Size(40, 20);

            chkVAC2ProcessedExclusiveOut = new System.Windows.Forms.CheckBoxTS();
            chkVAC2ProcessedExclusiveOut.Name = "chkVAC2ProcessedExclusiveOut";
            chkVAC2ProcessedExclusiveOut.Text = "Exclusive Out";
            chkVAC2ProcessedExclusiveOut.Location = new System.Drawing.Point(8, 19);
            chkVAC2ProcessedExclusiveOut.Size = new System.Drawing.Size(84, 17);
            chkVAC2ProcessedExclusiveOut.Checked = false;

            tpVAC2.Controls.Add(lblVAC2OperatingMode);
            tpVAC2.Controls.Add(comboVAC2OperatingMode);
            tpVAC2.Controls.Add(lblVAC2ProcessedModeNote);
            grpAudioDetails3.Controls.Add(lblVAC2ProcessedDriver);
            grpAudioDetails3.Controls.Add(comboVAC2ProcessedDriver);
            grpAudioDetails3.Controls.Add(lblVAC2ProcessedInput);
            grpAudioDetails3.Controls.Add(lblVAC2ProcessedOutput);
            grpAudioDetails3.Controls.Add(comboVAC2ProcessedOutput);
            grpAudioSampleRate3.Controls.Add(comboVAC2ProcessedSampleRate);
            grpAudioBuffer3.Controls.Add(comboVAC2ProcessedBuffer);
            grpVAC2Gain.Controls.Add(lblVAC2ProcessedGain);
            grpVAC2Gain.Controls.Add(udVAC2ProcessedTXGain);
            grpAudioStereo3.Controls.Add(chkVAC2ProcessedExclusiveOut);

            foreach (object host in Audio.GetPAHosts())
                comboVAC2ProcessedDriver.Items.Add(host);

            int defaultHost = 0;
            for (int i = 0; i < comboVAC2ProcessedDriver.Items.Count; i++)
                if (comboVAC2ProcessedDriver.Items[i].ToString().IndexOf("WASAPI", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    defaultHost = i;
                    break;
                }
            if (comboVAC2ProcessedDriver.Items.Count > 0)
                comboVAC2ProcessedDriver.SelectedIndex = defaultHost;
            PopulateVAC2ProcessedOutputs(true);

            comboVAC2OperatingMode.SelectedIndexChanged += comboVAC2OperatingMode_SelectedIndexChanged;
            comboVAC2ProcessedDriver.SelectedIndexChanged += comboVAC2ProcessedDriver_SelectedIndexChanged;
            comboVAC2ProcessedOutput.SelectedIndexChanged += comboVAC2ProcessedOutput_SelectedIndexChanged;
            comboVAC2ProcessedSampleRate.SelectedIndexChanged += comboVAC2ProcessedSettingChanged;
            comboVAC2ProcessedBuffer.SelectedIndexChanged += comboVAC2ProcessedSettingChanged;
            udVAC2ProcessedTXGain.ValueChanged += udVAC2ProcessedTXGain_ValueChanged;
            chkVAC2ProcessedExclusiveOut.CheckedChanged += chkVAC2ProcessedExclusiveOut_CheckedChanged;

            ApplyVAC2ProcessedSettings(false);
            UpdateVAC2OperatingModeUI(false);
        }

        private void PopulateVAC2ProcessedOutputs(bool chooseUSBCodec)
        {
            comboVAC2ProcessedOutput.Items.Clear();
            if (comboVAC2ProcessedDriver.SelectedIndex < 0) return;

            foreach (object device in Audio.GetPAOutputDevices(comboVAC2ProcessedDriver.SelectedIndex))
                comboVAC2ProcessedOutput.Items.Add(device);

            if (comboVAC2ProcessedOutput.Items.Count == 0) return;
            int selected = 0;
            if (chooseUSBCodec)
                for (int i = 0; i < comboVAC2ProcessedOutput.Items.Count; i++)
                    if (comboVAC2ProcessedOutput.Items[i].ToString().IndexOf("USB Audio CODEC", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        selected = i;
                        break;
                    }
            comboVAC2ProcessedOutput.SelectedIndex = selected;
        }

        private void ApplyVAC2ProcessedSettings(bool restart)
        {
            if (comboVAC2ProcessedDriver.SelectedIndex >= 0)
                Audio.VAC2ProcessedHost = comboVAC2ProcessedDriver.SelectedIndex;

            if (comboVAC2ProcessedOutput.SelectedItem is PADeviceInfo)
                Audio.VAC2ProcessedOutput = ((PADeviceInfo)comboVAC2ProcessedOutput.SelectedItem).Index;

            int value;
            if (comboVAC2ProcessedSampleRate.SelectedItem != null && Int32.TryParse(comboVAC2ProcessedSampleRate.SelectedItem.ToString(), out value))
                Audio.VAC2ProcessedSampleRate = value;
            if (comboVAC2ProcessedBuffer.SelectedItem != null && Int32.TryParse(comboVAC2ProcessedBuffer.SelectedItem.ToString(), out value))
                Audio.VAC2ProcessedBlockSize = value;

            Audio.VAC2ProcessedExclusiveOut = chkVAC2ProcessedExclusiveOut.Checked ? 1 : 0;
            Audio.VAC2ProcessedGainDB = (double)udVAC2ProcessedTXGain.Value;

            if (restart && Audio.VAC2ProcessedTXOutput)
                Audio.RestartVAC2();
        }

        private void comboVAC2OperatingMode_SelectedIndexChanged(object sender, EventArgs e)
        {
            bool processed = comboVAC2OperatingMode.SelectedIndex == 1;
            ApplyVAC2ProcessedSettings(false);
            UpdateVAC2OperatingModeUI(processed);
            Audio.VAC2ProcessedTXOutput = processed;
        }

        private void comboVAC2ProcessedDriver_SelectedIndexChanged(object sender, EventArgs e)
        {
            PopulateVAC2ProcessedOutputs(false);
            ApplyVAC2ProcessedSettings(true);
        }

        private void comboVAC2ProcessedOutput_SelectedIndexChanged(object sender, EventArgs e)
        {
            ApplyVAC2ProcessedSettings(true);
        }

        private void comboVAC2ProcessedSettingChanged(object sender, EventArgs e)
        {
            ApplyVAC2ProcessedSettings(true);
        }

        private void udVAC2ProcessedTXGain_ValueChanged(object sender, EventArgs e)
        {
            Audio.VAC2ProcessedGainDB = (double)udVAC2ProcessedTXGain.Value;
        }

        private void chkVAC2ProcessedExclusiveOut_CheckedChanged(object sender, EventArgs e)
        {
            ApplyVAC2ProcessedSettings(true);
        }

        private void UpdateVAC2OperatingModeUI(bool processed)
        {
            // Normal mode is deliberately the existing VAC2 UI and semantics.
            lblAudioDriver3.Visible = !processed;
            comboAudioDriver3.Visible = !processed;
            lblAudioInput3.Visible = !processed;
            comboAudioInput3.Visible = !processed;
            lblAudioOutput3.Visible = !processed;
            comboAudioOutput3.Visible = !processed;
            chkVAC2ExclusiveIn.Visible = !processed;
            chkVAC2ExclusiveOut.Visible = !processed;

            lblVAC2ProcessedDriver.Visible = processed;
            comboVAC2ProcessedDriver.Visible = processed;
            lblVAC2ProcessedInput.Visible = processed;
            lblVAC2ProcessedOutput.Visible = processed;
            comboVAC2ProcessedOutput.Visible = processed;

            comboAudioSampleRate3.Visible = !processed;
            comboVAC2ProcessedSampleRate.Visible = processed;
            comboAudioBuffer3.Visible = !processed;
            comboVAC2ProcessedBuffer.Visible = processed;

            lblVAC2GainRX.Visible = !processed;
            udVAC2GainRX.Visible = !processed;
            lblVAC2GainTX.Visible = !processed;
            udVAC2GainTX.Visible = !processed;
            lblVAC2ProcessedGain.Visible = processed;
            udVAC2ProcessedTXGain.Visible = processed;

            chkAudioStereo3.Visible = !processed;
            chkVAC2ProcessedExclusiveOut.Visible = processed;

            grpVAC2DirectIQ.Visible = !processed;
            grpVAC2LatencyManual.Visible = !processed;
            grpVAC2AutoEnable.Visible = !processed;
            chkVAC2onSplit.Visible = !processed;
            chkVAC2UseRX2.Visible = !processed;
            chkVAC2Combine.Visible = !processed;
            lblVAC2ProcessedModeNote.Visible = processed;

            grpAudioDetails3.Text = processed ? "Processed TX Output Setup" : "Virtual Audio Cable 2 Setup";
            grpAudioStereo3.Text = processed ? "WASAPI" : "Mono/Stereo";

            if (processed)
            {
                lblVAC2ProcessedDriver.BringToFront();
                comboVAC2ProcessedDriver.BringToFront();
                lblVAC2ProcessedInput.BringToFront();
                lblVAC2ProcessedOutput.BringToFront();
                comboVAC2ProcessedOutput.BringToFront();
                comboVAC2ProcessedSampleRate.BringToFront();
                comboVAC2ProcessedBuffer.BringToFront();
                lblVAC2ProcessedGain.BringToFront();
                udVAC2ProcessedTXGain.BringToFront();
                chkVAC2ProcessedExclusiveOut.BringToFront();
            }
        }

'''
text = once(text, method_anchor, methods + method_anchor, "setup.cs Phase 2 methods")
save(p, text, bom, crlf)

print("Applied full reversible Phase 2 patch")
