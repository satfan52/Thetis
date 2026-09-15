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

        private static RadioDSPRX ChObj(int rx, int sub)
        {
            return (_console != null && _console.radio != null) ? _console.radio.GetDSPRX(rx, sub) : null;
        }

        private static double MainHz(int rx)
        {
            var slice = HeadlessSliceManager.Instance.GetSlice(rx);
            return slice != null ? slice.FrequencyMHz * 1e6 : 0.0;
        }

        /// <summary>Enable/disable the subrx DSP channel. Initialises it from stored state.</summary>
        public static bool SetEnabled(int rx, bool on)
        {
            var s = Get(rx);
            var ch = ChObj(rx, 1);
            if (ch == null || _console == null) return false;
            try
            {
                if (!cmaster.IsRadioCreated) return false;
                if (on)
                {
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
                return true;
            }
            catch { return false; }
        }

        public static bool IsEnabled(int rx) { return Get(rx).Enabled; }

        /// <summary>VFO B absolute frequency (Hz). Main slice frequency is the DDC centre.</summary>
        public static void SetFreq(int rx, long hz)
        {
            Get(rx).FreqHz = hz;
            SetFreqInternal(rx, hz);
        }

        private static void SetFreqInternal(int rx, long hz)
        {
            var ch = ChObj(rx, 1);
            if (ch == null || hz <= 0) return;
            double offset = hz - MainHz(rx);           // signed offset from DDC centre
            double span = 48000.0 * 0.45;              // stay inside the DDC passband
            if (offset > span) offset = span;
            if (offset < -span) offset = -span;
            ch.RXOsc = -offset;                        // RadioDSPRX applies SetRXAShiftFreq(-RXOsc)
        }

        public static long GetFreq(int rx) { return Get(rx).FreqHz; }

        public static void ApplyMode(int rx, DSPMode mode)
        {
            var s = Get(rx); s.Mode = mode;
            var ch = ChObj(rx, 1);
            if (ch == null) return;
            ch.DSPMode = mode;                         // routes SetRXAMode(id(rx,1))
        }

        public static void ApplyFilter(int rx, int low, int high)
        {
            var s = Get(rx); s.FilterLow = low; s.FilterHigh = high;
            var ch = ChObj(rx, 1);
            if (ch == null) return;
            ch.RXFilterLow = low;
            ch.RXFilterHigh = high;                    // routes RXANBPSetFreqs on id(rx,1)
        }

        /// <summary>balance 0..1: sub panned to balance, main panned to 1-balance (0.5 = both centred).</summary>
        public static void ApplyBalance(int rx, double balance)
        {
            var s = Get(rx);
            s.Balance = Math.Max(0.0, Math.Min(1.0, balance));
            var main = ChObj(rx, 0); var sub = ChObj(rx, 1);
            if (main == null || sub == null) return;
            main.Pan = (float)(1.0 - s.Balance);
            sub.Pan = (float)s.Balance;
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
