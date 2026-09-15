using System;
using System.Collections.Generic;

namespace Thetis
{
    /// <summary>
    /// Branch H1: sub-receiver (VFO B) control for headless ports.
    ///
    /// Each headless DDC (RX2..RX8 on ports 50002..50008) runs two WDSP DSP
    /// channels (cmSubRCVR = 2): id(rx,0) = main (VFO A), id(rx,1) = sub
    /// (VFO B). Both demodulate the same DDC IQ stream - no extra DDC.
    ///
    /// Frequency model: the headless DDC is tuned so its centre equals the
    /// main slice frequency (HeadlessSliceManager.SetFrequency ->
    /// NetworkIO.VFOfreq). Main RXOsc stays 0; the sub channel is placed with
    /// RXOsc = -(subHz - mainHz), matching Thetis's own subrx convention
    /// (SetRXAShiftFreq receives -RXOsc, so the effective shift is +offset).
    ///
    /// Audio selection without native changes: each DSP channel has a panel
    /// pan (SetRXAPanelPan: 0 = left only, 1 = right only). pipe.c sums the
    /// two channels into the TCI audio L/R pair, so main hard-left and sub
    /// hard-right lets the client pick main / sub / both by ears, with a
    /// balance control in between.
    /// </summary>
    public static class HeadlessSubRX
    {
        public class State
        {
            public bool Enabled;
            public long FreqHz;
            public DSPMode Mode = DSPMode.USB;
            public int FilterLow = 150;
            public int FilterHigh = 2800;
            public double Balance = 1.0;          // sub pan position 0..1 (main gets 1 - balance)
            public bool Nr, Anf, Snb;
            public AGCMode AgcMode = AGCMode.MED;
            public double AgcGainDb = 20.0;
        }

        private static readonly Dictionary<int, State> _states = new Dictionary<int, State>();
        private static readonly object _lock = new object();
        private static Console _console;

        public static void Initialize(Console console) { _console = console; }

        private static int Ch(int rx, int sub) { return 2 * rx + sub; }

        public static State Get(int rx)
        {
            lock (_lock)
            {
                if (!_states.TryGetValue(rx, out var s)) { s = new State(); _states[rx] = s; }
                return s;
            }
        }

        // NOTE: radio.GetDSPRX only covers threads 0..1 (console RX1/RX2).
        // Headless channels (thread = rx index 1..7) are driven via WDSP calls
        // directly, exactly like HeadlessSliceManager does for the main channel.

        private static double MainHz(int rx)
        {
            // B's shift is relative to the DDC centre (hardware centre frequency),
            // which under CTUN may differ from VFO A.
            return HeadlessSliceManager.Instance.GetDisplayCenterMHz(rx) * 1e6;
        }

        /// <summary>Enable/disable the subrx DSP channel. Initialises it from stored state.</summary>
        public static bool SetEnabled(int rx, bool on)
        {
            var s = Get(rx);
            try
            {
                if (!cmaster.IsRadioCreated) return false;
                if (on)
                {
                    // start the sub with the MAIN slice's current filter so both
                    // VFOs sound identical until the user changes B explicitly
                    var mainSlice = HeadlessSliceManager.Instance.GetSlice(rx);
                    if (mainSlice != null)
                    {
                        s.FilterLow = mainSlice.FilterLow;
                        s.FilterHigh = mainSlice.FilterHigh;
                    }
                    // full channel init (same pattern as HeadlessSliceManager.ActivateAudio)
                    WDSP.SetRXAMode(Ch(rx, 1), s.Mode);
                    WDSP.SetRXABandpassFreqs(Ch(rx, 1), s.FilterLow, s.FilterHigh);
                    WDSP.RXANBPSetFreqs(Ch(rx, 1), s.FilterLow, s.FilterHigh);
                    WDSP.SetRXASNBAOutputBandwidth(Ch(rx, 1), s.FilterLow, s.FilterHigh);
                    WDSP.SetRXAAGCMode(Ch(rx, 1), s.AgcMode);
                    WDSP.SetRXAAGCTop(Ch(rx, 1), 90.0);
                    WDSP.SetRXAAGCFixed(Ch(rx, 1), s.AgcGainDb);
                    WDSP.SetRXAPanelGain1(Ch(rx, 1), 0.5);
                    ApplyBalance(rx, s.Balance);
                    SetFreqInternal(rx, s.FreqHz);
                    if (s.Nr) WDSP.SetRXAANRRun(Ch(rx, 1), 1);
                    if (s.Anf) WDSP.SetRXAANFRun(Ch(rx, 1), true);
                    if (s.Snb) WDSP.SetRXASNBARun(Ch(rx, 1), true);
                }
                WDSP.SetChannelState(Ch(rx, 1), on ? 1 : 0, on ? 0 : 1);
                s.Enabled = on;
                TciLog.Log($"[SubRX] rx{rx} enabled={on} ok");
                return true;
            }
            catch (Exception ex)
            {
                TciLog.Log($"[SubRX] rx{rx} enable FAILED: {ex.GetType().Name}: {ex.Message}");
                return false;
            }
        }

        public static bool IsEnabled(int rx) { return Get(rx).Enabled; }

        /// <summary>VFO B absolute frequency (Hz). Main slice frequency is the DDC centre.</summary>
        public static void SetFreq(int rx, long hz)
        {
            Get(rx).FreqHz = hz;
            SetFreqInternal(rx, hz);
        }

        private static bool UpperSideband(DSPMode m)
        {
            switch (m)
            {
                case DSPMode.LSB:
                case DSPMode.DIGL:
                case DSPMode.CWL:
                    return false;
                default:
                    return true;   // USB/DIGU/CWU/AM/SAM/FM...
            }
        }

        private static void SetFreqInternal(int rx, long hz)
        {
            if (hz <= 0 || !cmaster.IsRadioCreated) return;
            var st = Get(rx);
            double main = MainHz(rx);                  // DDC centre (Hz)
            double offset = hz - main;                 // signed offset from DDC centre
            double edge = 48000.0;                     // hard DDC edge (rate/2)
            // Filter-aware passband edges: the carrier may reach the DDC edge
            // only where its passband does not extend past it (USB sits above
            // the carrier, LSB below, AM/SAM/NFM symmetric).
            double upper = edge - Math.Max(0.0, (double)st.FilterHigh);
            double lower = -edge + Math.Max(0.0, (double)(-st.FilterLow));
            double raw = offset;
            if (offset > upper) offset = upper;
            if (offset < lower) offset = lower;
            if (Math.Abs(raw - offset) > 0.5)
                TciLog.Log($"[SubFreq] rx{rx} req={hz} main={main:0} filt={st.FilterLow}/{st.FilterHigh} off={raw:0} -> {offset:0} (up={upper:0} lo={lower:0})");
            // Thetis convention (txtVFOBFreq handler): RXOsc_sub = -(fB - fA) and
            // RadioDSPRX applies SetRXAShiftFreq(-RXOsc) => effective shift = +(fB - fA).
            // We drive SetRXAShiftFreq directly, so pass the offset itself.
            WDSP.SetRXAShiftFreq(Ch(rx, 1), offset);
            WDSP.RXANBPSetShiftFrequency(Ch(rx, 1), offset);
        }

        public static long GetFreq(int rx) { return Get(rx).FreqHz; }

        public static void ApplyMode(int rx, DSPMode mode)
        {
            var st = Get(rx); st.Mode = mode;
            if (!cmaster.IsRadioCreated)
            {
                TciLog.Log($"[SubRX] rx{rx} ApplyMode {mode} SKIPPED - radio not created");
                return;
            }
            int ch = Ch(rx, 1);
            // Replicate Thetis's SetRX1Mode sequence for a mode change:
            // DSP channel OFF -> SetRXAMode -> filter re-apply -> channel ON.
            // (Thetis powers the channels down around every mode change; without
            // the off/on cycle the demodulator does not reliably switch sideband.)
            int prevState = WDSP.SetChannelState(ch, 0, 1);
            WDSP.SetDSPSamplerate(ch, 48000);          // Thetis always does this in SetRX1Mode
            WDSP.SetRXAMode(ch, mode);
            WDSP.SetRXABandpassFreqs(ch, st.FilterLow, st.FilterHigh);
            WDSP.RXANBPSetFreqs(ch, st.FilterLow, st.FilterHigh);
            WDSP.SetRXASNBAOutputBandwidth(ch, st.FilterLow, st.FilterHigh);
            if (st.Enabled || prevState == 1)
                WDSP.SetChannelState(ch, 1, 0);
            TciLog.Log($"[SubRX] rx{rx} ApplyMode {mode} ch={ch} filt={st.FilterLow}-{st.FilterHigh}");
        }

        public static void ApplyFilter(int rx, int low, int high)
        {
            var s = Get(rx); s.FilterLow = low; s.FilterHigh = high;
            if (!cmaster.IsRadioCreated) return;
            WDSP.SetRXABandpassFreqs(Ch(rx, 1), low, high);
            WDSP.RXANBPSetFreqs(Ch(rx, 1), low, high);
            WDSP.SetRXASNBAOutputBandwidth(Ch(rx, 1), low, high);
        }

        /// <summary>
        /// Branch H1: main/sub level control via per-channel panel GAIN, not pan.
        /// The WDSP pan law (sin) attenuates near its extremes and treats the
        /// mono demod audio asymmetrically (L=I, R=sin*Q), which made VFO B sound
        /// different from VFO A. Balance now scales the two channels' gains
        /// around unity (constant loudness), both channels panned centre.
        /// balance 0 = main only, 0.5 = equal, 1 = sub only.
        /// </summary>
        public static void ApplyBalance(int rx, double balance)
        {
            var s = Get(rx);
            s.Balance = Math.Max(0.0, Math.Min(1.0, balance));
            if (!cmaster.IsRadioCreated) return;
            double gMain = Math.Cos(s.Balance * Math.PI / 2.0);   // 1 .. 0
            double gSub  = Math.Sin(s.Balance * Math.PI / 2.0);   // 0 .. 1
            WDSP.SetRXAPanelPan(Ch(rx, 0), 0.5);
            WDSP.SetRXAPanelPan(Ch(rx, 1), 0.5);
            WDSP.SetRXAPanelGain1(Ch(rx, 0), 0.5 * 2.0 * gMain);  // slice gain 0.5 baseline
            WDSP.SetRXAPanelGain1(Ch(rx, 1), 0.5 * 2.0 * gSub);
        }

        public static void ApplyAgc(int rx, AGCMode mode, double fixedDb)
        {
            var s = Get(rx); s.AgcMode = mode; s.AgcGainDb = fixedDb;
            WDSP.SetRXAAGCMode(Ch(rx, 1), mode);
            WDSP.SetRXAAGCFixed(Ch(rx, 1), fixedDb);
        }

        /// <summary>Wideband NB acts on the shared DDC stream (before the main/sub split) - one setting serves both.</summary>
        public static void SetNb(int rx, bool on)
        {
            cmaster.SetRCVRANBRun(0, rx, on);          // NB is DDC-wide (before main/sub split)
        }

        public static void SetNr(int rx, bool on) { Get(rx).Nr = on; WDSP.SetRXAANRRun(Ch(rx, 1), on ? 1 : 0); }
        public static void SetAnf(int rx, bool on) { Get(rx).Anf = on; WDSP.SetRXAANFRun(Ch(rx, 1), on); }
        public static void SetSnb(int rx, bool on) { Get(rx).Snb = on; WDSP.SetRXASNBARun(Ch(rx, 1), on); }
    }
}
