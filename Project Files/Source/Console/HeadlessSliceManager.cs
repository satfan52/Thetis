//=================================================================
// HeadlessSliceManager.cs
//=================================================================
// Manages dormant, on-demand headless slices (RX3..RX8 / DDC 2..7)
// for digital operations in Release F.
// Slices consume 0% DSP CPU when not actively streaming audio.
//=================================================================

using System;
using System.Collections.Generic;
using System.Runtime.ExceptionServices;
using System.Security;

namespace Thetis
{
    public sealed class HeadlessSlice
    {
        public int RxIndex { get; }
        public int ChannelId { get; }
        public double FrequencyMHz { get; set; } = 14.074;
        public DSPMode Mode { get; set; } = DSPMode.DIGU;
        public int FilterLow { get; set; } = 300;
        public int FilterHigh { get; set; } = 3000;
        public bool IsActive { get; set; } = false;
        public bool IsStreamingAudio { get; set; } = false;
        // Branch G: IQ streaming is requested independently of audio
        public bool IsStreamingIQ { get; set; } = false;
        public double AudioGain { get; set; } = 0.5;   // Thetis-like default AF level (-6 dBFS panel gain)
        public AGCMode AgcMode { get; set; } = AGCMode.MED;   // user-selected AGC preset

        public HeadlessSlice(int rxIndex)
        {
            RxIndex = rxIndex;
            // ChannelMaster/cmsetup.c: chid(stream, subrx) = cmSubRCVR * stream + subrx = 2 * rxIndex
            ChannelId = 2 * rxIndex;
        }
    }

    public sealed class HeadlessSliceManager
    {
        public static HeadlessSliceManager Instance { get; } = new HeadlessSliceManager();

        public event Action<int, double> SliceFrequencyChanged;
        public event Action<int, DSPMode> SliceModeChanged;
        public event Action<int, int, int> SliceFilterChanged;
        public event Action<int, bool> SliceStreamingChanged;

        private readonly Dictionary<int, HeadlessSlice> _slices = new Dictionary<int, HeadlessSlice>();
        private readonly object _lock = new object();

        private HeadlessSliceManager()
        {
            // Headless slices 1..7 correspond to DDC 1..7 (RX2..RX8)
            for (int rx = 1; rx < 8; rx++)
            {
                _slices[rx] = new HeadlessSlice(rx);
            }
        }

        public HeadlessSlice GetSlice(int rx)
        {
            lock (_lock)
            {
                if (_slices.TryGetValue(rx, out HeadlessSlice slice))
                    return slice;
                return null;
            }
        }

        public List<HeadlessSlice> GetAllSlices()
        {
            lock (_lock)
            {
                return new List<HeadlessSlice>(_slices.Values);
            }
        }

        // Branch G fix: DDC-to-RX mapping must match Thetis's NCO assignment and
        // the router table (see cmaster.CMLoadRouterAll). Thetis uses:
        //   DDC0 = RX1 (VFO A), DDC3 = RX2 (VFO B).
        // Headless slices use the remaining DDCs:
        //   headless rx 1 (RX2)  -> DDC3 (matches Thetis's RX2)
        //   headless rx 2 (RX3)  -> DDC1
        //   headless rx 3 (RX4)  -> DDC2
        //   headless rx 4 (RX5)  -> DDC4
        //   headless rx 5 (RX6)  -> DDC5
        //   headless rx 6 (RX7)  -> DDC6
        //   headless rx 7 (RX8)  -> DDC7
        private static readonly int[] RxToDdc = new int[8] { -1, 3, 1, 2, 4, 5, 6, 7 };

        public static int GetDdcForRx(int rx)
        {
            return (rx >= 0 && rx < 8) ? RxToDdc[rx] : -1;
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void SetFrequency(int rx, double freqMHz)
        {
            HeadlessSlice slice;
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out slice)) return;
                slice.FrequencyMHz = freqMHz;
            }

            if (cmaster.IsRadioCreated)
            {
                try
                {
                    int ddc = GetDdcForRx(rx);
                    if (ddc >= 0)
                        NetworkIO.VFOfreq(ddc, freqMHz, 0);
                }
                catch { }
            }

            // Branch H1: the DDC centre is the master VFO; keep it in sync
            _displayCenterMHz[rx] = freqMHz;

            SliceFrequencyChanged?.Invoke(rx, freqMHz);
        }

        // Branch H1: per-rx DDC centre = the hidden 'master VFO'. VFO A floats
        // inside the DDC passband via the main channel's RXOsc (exactly like
        // Thetis's CentreFrequency + RXOsc model in CTUN); the DDC is retuned
        // only when A would leave the passband (scroll/re-centre).
        private readonly Dictionary<int, double> _displayCenterMHz = new Dictionary<int, double>();

        public double GetDisplayCenterMHz(int rx)
        {
            lock (_lock)
            {
                if (_displayCenterMHz.TryGetValue(rx, out double v)) return v;
                var slice = GetSlice(rx);
                double f = slice != null ? slice.FrequencyMHz : 14.074;
                _displayCenterMHz[rx] = f;
                return f;
            }
        }

        /// <summary>Branch H1: move VFO A without retuning the DDC (CTUN-style).
        /// Re-centres the DDC only when A would leave the passband. Returns the
        /// display centre actually in effect after the move.</summary>
        public double SetVFOA(int rx, double freqMHz)
        {
            HeadlessSlice slice;
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out slice)) return freqMHz;
            }
            if (!cmaster.IsRadioCreated) return freqMHz;

            double center = GetDisplayCenterMHz(rx);
            const double rate = 96000.0;
            double halfSpan = rate / 2.0;
            double margin = rate * 0.04;                    // Thetis-style 4% edge margin
            double maxOff = halfSpan - margin;
            double offsetHz = (freqMHz - center) * 1e6;

            if (Math.Abs(offsetHz) > halfSpan)
            {
                // A left the DDC entirely: nothing else possible, re-centre onto A
                center = freqMHz;
                _displayCenterMHz[rx] = center;
                SetFrequency(rx, freqMHz);
            }
            else if (Math.Abs(offsetHz) > maxOff)
            {
                // A reached the passband working edge: SCROLL the DDC by the
                // minimum amount needed to bring A back inside the margin
                // (Thetis scrolls gradually, lines 'scroll the spectrum display
                // smoothly at the edge'), never jumping onto A.
                double excess = Math.Abs(offsetHz) - maxOff;
                double scrollHz = offsetHz > 0 ? excess : -excess;
                center += scrollHz / 1e6;
                _displayCenterMHz[rx] = center;
                SetFrequency(rx, center);
                offsetHz = (freqMHz - center) * 1e6;        // now == maxOff*sign
            }

            if (Math.Abs(offsetHz) <= maxOff)
            {
                // A floats via RXOsc (shift = +(A - centre)); DDC untouched
                slice.FrequencyMHz = freqMHz;
                WDSP.SetRXAShiftFreq(2 * rx, offsetHz);
                WDSP.RXANBPSetShiftFrequency(2 * rx, offsetHz);
            }

            // B is stored as an absolute frequency: re-apply it against the new
            // centre so it never slides when the DDC moves.
            try
            {
                long bHz = HeadlessSubRX.GetFreq(rx);
                if (HeadlessSubRX.IsEnabled(rx) && bHz > 0)
                    HeadlessSubRX.SetFreq(rx, bHz);
            }
            catch { }

            SliceFrequencyChanged?.Invoke(rx, freqMHz);
            return center;
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void SetMode(int rx, DSPMode mode)
        {
            HeadlessSlice slice;
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out slice)) return;
                slice.Mode = mode;
                if (slice.IsActive && cmaster.IsRadioCreated)
                {
                    try
                    {
                        WDSP.SetRXAMode(slice.ChannelId, mode);
                    }
                    catch { }
                }
            }

            SliceModeChanged?.Invoke(rx, mode);
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void SetFilter(int rx, int lowHz, int highHz)
        {
            HeadlessSlice slice;
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out slice)) return;
                slice.FilterLow = lowHz;
                slice.FilterHigh = highHz;
                if (slice.IsActive && cmaster.IsRadioCreated)
                {
                    try
                    {
                        WDSP.SetRXABandpassFreqs(slice.ChannelId, lowHz, highHz);
                        WDSP.RXANBPSetFreqs(slice.ChannelId, lowHz, highHz);
                        WDSP.SetRXASNBAOutputBandwidth(slice.ChannelId, lowHz, highHz);
                    }
                    catch { }
                }
            }

            SliceFilterChanged?.Invoke(rx, lowHz, highHz);
        }

        public void SetSliceGain(int rx, double gain)
        {
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return;
                slice.AudioGain = Math.Max(0.001, Math.Min(2.0, gain));
                if (slice.IsActive && cmaster.IsRadioCreated)
                {
                    try
                    {
                        WDSP.SetRXAPanelGain1(slice.ChannelId, slice.AudioGain);
                    }
                    catch { }
                }
            }
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void ActivateAudio(int rx)
        {
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return;

                if (!slice.IsActive)
                {
                    if (cmaster.IsRadioCreated)
                    {
                        try
                        {
                            // Ensure ChannelMaster router has all 8 sources active
                            cmaster.CMLoadRouterAll(HardwareSpecific.Model);

                            // Dynamically query hardware sample rate
                            int inRate = cmaster.GetInputRate(0, 0);
                            if (inRate <= 0) inRate = 48000;
                            int ddc = GetDdcForRx(rx);
                            if (ddc >= 0) NetworkIO.SetDDCRate(ddc, inRate);
                            cmaster.SetXcmInrate(rx, inRate);

                            // Configure DSP parameters for slice channel
                            WDSP.SetRXAMode(slice.ChannelId, slice.Mode);
                            WDSP.SetRXABandpassFreqs(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                            WDSP.RXANBPSetFreqs(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                            WDSP.SetRXASNBAOutputBandwidth(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                            WDSP.SetRXAAGCMode(slice.ChannelId, AGCMode.MED);
                            WDSP.SetRXAAGCTop(slice.ChannelId, 90.0);
                            WDSP.SetRXAPanelGain1(slice.ChannelId, slice.AudioGain);

                            // Turn ON WDSP processing for this slice channel
                            WDSP.SetChannelState(slice.ChannelId, 1, 0);
                        }
                        catch { }
                    }
                    slice.IsActive = true;
                }

                slice.IsStreamingAudio = true;

                if (cmaster.IsRadioCreated)
                {
                    try
                    {
                        int ddcVF = GetDdcForRx(rx);
                        if (ddcVF >= 0) NetworkIO.VFOfreq(ddcVF, slice.FrequencyMHz, 0);
                    }
                    catch { }

                    try
                    {
                        cmaster.SetRXTCIRun(1);
                        cmaster.UpdateRXTCIRunState();
                    }
                    catch { }
                }
            }

            SliceStreamingChanged?.Invoke(rx, true);
        }

        // Branch G: activate a slice for IQ streaming only (no audio client).
        // CW Skimmer requests IQ without audio; the WDSP channel and DDC must be
        // running for IQ to flow, so this shares the same activation core.
        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void ActivateIQ(int rx)
        {
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return;
                slice.IsStreamingIQ = true;
                if (slice.IsActive) return;
            }
            // reuse the audio activation path (idempotent - it just flags streaming audio
            // true as well, which is harmless; the client simply never consumes it)
            ActivateAudio(rx);
            lock (_lock)
            {
                if (_slices.TryGetValue(rx, out HeadlessSlice s2))
                {
                    s2.IsStreamingAudio = false;
                    s2.IsStreamingIQ = true;
                }
            }
        }

        // Branch G: called when the last IQ client disconnects/stops.
        // Deactivates the slice only if audio is not streaming either.
        public void DeactivateIQ(int rx)
        {
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return;
                slice.IsStreamingIQ = false;
                if (slice.IsStreamingAudio) return;
            }
            DeactivateAudio(rx);
        }

        public bool IsAnyStreaming
        {
            get
            {
                lock (_lock)
                {
                    foreach (var s in _slices.Values)
                    {
                        if (s.IsStreamingAudio) return true;
                    }
                    return false;
                }
            }
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void SyncActiveSlices()
        {
            lock (_lock)
            {
                if (!cmaster.IsRadioCreated) return;
                foreach (var s in _slices.Values)
                {
                    if (s.IsStreamingAudio)
                    {
                        s.IsActive = false;
                        ActivateAudio(s.RxIndex);
                    }
                }
            }
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void DeactivateAudio(int rx)
        {
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return;

                slice.IsStreamingAudio = false;
                // Branch G: keep the WDSP channel alive while IQ streaming continues
                if (slice.IsStreamingIQ) return;

                if (slice.IsActive)
                {
                    if (cmaster.IsRadioCreated)
                    {
                        try
                        {
                            // Turn OFF WDSP processing to immediately reclaim 0% CPU
                            WDSP.SetChannelState(slice.ChannelId, 0, 0);
                        }
                        catch { }
                    }
                    slice.IsActive = false;
                }

                if (cmaster.IsRadioCreated)
                {
                    try
                    {
                        cmaster.UpdateRXTCIRunState();
                    }
                    catch { }
                }
            }

            SliceStreamingChanged?.Invoke(rx, false);
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void DeactivateAll()
        {
            lock (_lock)
            {
                foreach (var kvp in _slices)
                {
                    kvp.Value.IsStreamingAudio = false;
                    if (kvp.Value.IsActive)
                    {
                        if (cmaster.IsRadioCreated)
                        {
                            try
                            {
                                WDSP.SetChannelState(kvp.Value.ChannelId, 0, 0);
                            }
                            catch { }
                        }
                        kvp.Value.IsActive = false;
                    }
                }
            }

            SliceStreamingChanged?.Invoke(-1, false);
        }
    }
}
