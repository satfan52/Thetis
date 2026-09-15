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

            // Branch H1: DDC centre follows in the classic model
            _displayCenterMHz[rx] = freqMHz;

            SliceFrequencyChanged?.Invoke(rx, freqMHz);
        }

        // Branch H1: per-rx DDC centre - the 'hardware centre frequency' (the
        // middle of the DDS passband), Thetis's RX1DDSFreq/CentreFrequency pair.
        private readonly Dictionary<int, double> _displayCenterMHz = new Dictionary<int, double>();
        // client display model: false = classic (DDC centred on A on every move),
        // true = CTUN (A floats inside the DDC via the main channel RXOsc)
        private readonly Dictionary<int, bool> _ctunMode = new Dictionary<int, bool>();

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

        public void SetCtunMode(int rx, bool ctun)
        {
            bool was;
            lock (_lock) { _ctunMode.TryGetValue(rx, out was); _ctunMode[rx] = ctun; }
            // leaving CTUN: re-centre the DDC onto A (classic model restored)
            if (was && !ctun)
            {
                var slice = GetSlice(rx);
                if (slice != null) SetFrequency(rx, slice.FrequencyMHz);
            }
        }

        /// <summary>Branch H1: move the DDC centre (hardware centre frequency)
        /// while keeping VFO A's absolute frequency (TCIServer 50001 dds semantics).</summary>
        public void SetDDCCenter(int rx, double centerMHz)
        {
            lock (_lock) { _displayCenterMHz[rx] = centerMHz; }
            if (!cmaster.IsRadioCreated) return;
            try
            {
                int ddc = GetDdcForRx(rx);
                if (ddc >= 0) NetworkIO.VFOfreq(ddc, centerMHz, 0);
            }
            catch { }
            // re-apply A (RXOsc = A - new centre) and B (absolute)
            var slice = GetSlice(rx);
            if (slice != null)
            {
                double offHz = (slice.FrequencyMHz - centerMHz) * 1e6;
                int mainCh = 2 * rx;
                WDSP.SetRXAShiftFreq(mainCh, offHz);
                WDSP.RXANBPSetShiftFrequency(mainCh, offHz);
            }
            try
            {
                long bHz = HeadlessSubRX.GetFreq(rx);
                if (HeadlessSubRX.IsEnabled(rx) && bHz > 0)
                    HeadlessSubRX.SetFreq(rx, bHz);
            }
            catch { }
        }

        /// <summary>
        /// Branch H1: set VFO A. Non-CTUN: the DDC is re-centred onto A (classic).
        /// CTUN: A moves via the main channel RXOsc inside the DDC; the DDC scrolls
        /// only when A would leave the passband. Returns the DDC centre in effect.
        /// </summary>
        public double SetVFOA(int rx, double freqMHz)
        {
            HeadlessSlice slice;
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out slice)) return freqMHz;
                slice.FrequencyMHz = freqMHz;      // A is the radio's tuned frequency (CI-V, TX)
            }
            if (!cmaster.IsRadioCreated) return freqMHz;

            bool ctun;
            lock (_lock) { _ctunMode.TryGetValue(rx, out ctun); }

            int mainCh = 2 * rx;                   // WDSP.id(rx, 0)
            double center = GetDisplayCenterMHz(rx);
            double offsetHz = (freqMHz - center) * 1e6;
            const double rate = 96000.0;
            double edge = rate / 2.0;
            double margin = rate * 0.04;           // working margin inside the edge

            if (!ctun)
            {
                // classic: DDC centre = A
                TciLog.Log($"[VFOA] rx{rx} classic retune DDC->{freqMHz:0.000000}");
                center = freqMHz;
                _displayCenterMHz[rx] = center;
                int ddc = GetDdcForRx(rx);
                if (ddc >= 0) NetworkIO.VFOfreq(ddc, freqMHz, 0);
                WDSP.SetRXAShiftFreq(mainCh, 0.0);
                WDSP.RXANBPSetShiftFrequency(mainCh, 0.0);
            }
            else
            {
                TciLog.Log($"[VFOA] rx{rx} ctun A={freqMHz:0.000000} centre={center:0.000000} off={offsetHz:0}");
                if (Math.Abs(offsetHz) > edge - margin)
                {
                    // A would leave the passband: scroll the DDC by the minimum
                    double excess = Math.Abs(offsetHz) - (edge - margin);
                    center += (offsetHz > 0 ? excess : -excess) / 1e6;
                    _displayCenterMHz[rx] = center;
                    int ddc = GetDdcForRx(rx);
                    if (ddc >= 0) NetworkIO.VFOfreq(ddc, center, 0);
                    offsetHz = (freqMHz - center) * 1e6;
                    TciLog.Log($"[VFOA] rx{rx} ctun SCROLL centre->{center:0.000000}");
                }
                WDSP.SetRXAShiftFreq(mainCh, offsetHz);
                WDSP.RXANBPSetShiftFrequency(mainCh, offsetHz);
            }

            // VFO B is stored as an absolute frequency: re-apply against the new centre
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
