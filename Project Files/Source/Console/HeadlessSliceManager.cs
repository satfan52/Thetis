//=================================================================
// HeadlessSliceManager.cs
//=================================================================
// Manages dormant, on-demand headless slices (RX3..RX8 / DDC 2..7)
// for digital operations in Release F.
// Slices consume 0% DSP CPU when not actively streaming audio.
//
// H1 (Policy A - shared control): the full-window GUI cells and the TCI
// servers are front-ends to the SAME receiver (hardware DDC NCO + WDSP
// channel 2*rx + analyser).  Each front-end registers a consumer, and a
// slice is activated by its first consumer and only deactivated when the
// last one releases it.  Closing a TCI audio client can therefore no
// longer switch off a slice the GUI view is still displaying, and the
// six spare DDCs can be activated for the GUI without pretending to be
// TCI audio streams.
//=================================================================

using System;
using System.Collections.Generic;
using System.Runtime.ExceptionServices;
using System.Security;

namespace Thetis
{
    // H1 (Policy A): one bit per front-end that keeps a slice alive.
    // A kind is present or absent - its owner guards its own add/remove
    // pairs (the TCI servers track that per client and per stream), so a
    // second audio client on the same port must not bump a counter that a
    // guard-gated stop would then fail to decrement.
    [Flags]
    public enum SliceConsumer
    {
        None = 0,
        Display = 1,      // the full-window GUI cell (needs WDSP running for its S-meter)
        GuiAudio = 2,     // the console's own audio path
        TciAudio = 4,     // a TCI client streaming audio
        TciIq = 8         // a TCI/CW Skimmer client streaming IQ
    }

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
        // H1: the DDC rate this slice was last activated at. Only a fallback for
        // the live query in GetSliceRateHz when ChannelMaster cannot answer.
        public int SampleRateHz { get; set; } = 0;

        // H1 (Policy A): the set of live consumers.  Only HeadlessSliceManager
        // touches this, always under its lock, hence no locking here.
        private int _consumerFlags;

        public HeadlessSlice(int rxIndex)
        {
            RxIndex = rxIndex;
            // ChannelMaster/cmsetup.c: chid(stream, subrx) = cmSubRCVR * stream + subrx = 2 * rxIndex
            ChannelId = 2 * rxIndex;
        }

        public bool AddConsumer(SliceConsumer consumer)
        {
            int before = _consumerFlags;
            _consumerFlags |= (int)consumer;
            return _consumerFlags != before;
        }

        public bool RemoveConsumer(SliceConsumer consumer)
        {
            int before = _consumerFlags;
            _consumerFlags &= ~(int)consumer;
            return _consumerFlags != before;
        }

        public bool HasConsumer(SliceConsumer consumer)
        {
            return (_consumerFlags & (int)consumer) != 0;
        }

        public bool HasAnyConsumer
        {
            get { return _consumerFlags != 0; }
        }

        public bool HasAudioConsumer
        {
            get { return (_consumerFlags & (int)(SliceConsumer.GuiAudio | SliceConsumer.TciAudio)) != 0; }
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

        // H1: the real DDC sample rate Thetis is running at. Thetis allows
        // 48 kHz .. 384 kHz and the 8-DDC firmware runs every DDC at RX1's rate;
        // the CTUN passband edge (rate/2), the VFO B clamp and the activation
        // rate must all follow it. This is queried live - never a constant -
        // because the user can change the rate between activations:
        //   1. ChannelMaster's own value for stream 0 (set from SampleRateRX1)
        //   2. the console's mirrored sample-rate setting
        //   3. the rate this slice was last activated with
        //   4. 48000, only if nothing else is known (initial startup)
        public int GetSliceRateHz(int rx)
        {
            int rate = 0;
            try
            {
                if (cmaster.IsRadioCreated) rate = cmaster.GetInputRate(0, 0);
            }
            catch { }
            if (rate <= 0)
            {
                try { rate = Display.SampleRateRX1; } catch { }
            }
            if (rate <= 0)
            {
                HeadlessSlice slice = GetSlice(rx);
                if (slice != null) rate = slice.SampleRateHz;
            }
            if (rate <= 0) rate = 48000;
            return rate;
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
            // H1: the DDC passband edge is the real rate's Nyquist (48k..384k),
            // never a constant - the station may run the DDCs at any Thetis rate.
            double rate = GetSliceRateHz(rx);
            double edge = rate / 2.0;

            if (!ctun)
            {
                // classic: DDC centre = A
                TciLog.Log($"[VFOA] rx{rx} classic retune DDC->{freqMHz:0.000000} rate={rate}");
                center = freqMHz;
                _displayCenterMHz[rx] = center;
                int ddc = GetDdcForRx(rx);
                if (ddc >= 0) NetworkIO.VFOfreq(ddc, freqMHz, 0);
                WDSP.SetRXAShiftFreq(mainCh, 0.0);
                WDSP.RXANBPSetShiftFrequency(mainCh, 0.0);
            }
            else
            {
                // Filter-aware passband edges for the MAIN channel (VFO A): A
                // may sit at the DDC edge only where its passband does not
                // extend past it (USB sits above, LSB below, AM/SAM/NFM
                // symmetric). Matches Thetis's rate/2 minus filter extent.
                var sliceF = GetSlice(rx);
                double upExt = Math.Max(0.0, (double)(sliceF != null ? sliceF.FilterHigh : 3000));
                double loExt = Math.Max(0.0, (double)(sliceF != null ? -sliceF.FilterLow : 300));
                double upper = edge - upExt;
                double lower = -edge + loExt;
                TciLog.Log($"[VFOA] rx{rx} ctun A={freqMHz:0.000000} centre={center:0.000000} off={offsetHz:0} up={upper:0} lo={lower:0} rate={rate}");
                if (offsetHz > upper || offsetHz < lower)
                {
                    // A reached/crossed the passband edge: the DDC JUMPS so VFO A
                    // becomes the new hardware centre (Thetis CTUN behaviour -
                    // the DDS stays put until the edge, then VFO A's frequency
                    // becomes the new centre and the waterfall jumps with it).
                    center = freqMHz;
                    _displayCenterMHz[rx] = center;
                    int ddc = GetDdcForRx(rx);
                    if (ddc >= 0) NetworkIO.VFOfreq(ddc, center, 0);
                    offsetHz = 0.0;
                    TciLog.Log($"[VFOA] rx{rx} ctun JUMP centre->{center:0.000000}");
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

        // ---- consumers -------------------------------------------------------------
        // H1 (Policy A): each front-end registers here.  The last release turns
        // the WDSP channel off; every earlier release leaves it running.

        /// <summary>Add one consumer of the slice. Returns true when the slice's
        /// consumer set actually changed (an idempotent add returns false).</summary>
        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public bool AddConsumer(int rx, SliceConsumer consumer)
        {
            bool audio;
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return false;
                if (!slice.AddConsumer(consumer)) return false;

                if (!slice.IsActive) EnsureActiveCoreLocked(slice);

                // H1: the streaming flags MUST be current before the TCI run state
                // is recomputed - UpdateRXTCIRunState reads IsAnyStreaming, so with
                // the old ActivateAudio order (flag first, run second) the tap was
                // switched on; doing it the other way round left tci_rx_out_run at 0
                // and the client received no frames at all.
                slice.IsStreamingAudio = slice.HasAudioConsumer;
                slice.IsStreamingIQ = slice.HasConsumer(SliceConsumer.TciIq);

                if ((consumer & (SliceConsumer.TciAudio | SliceConsumer.TciIq)) != 0)
                {
                    // The native TCI RX tap (audio AND IQ) is gated by tci_rx_out_run,
                    // which is what ActivateAudio/ActivateIQ used to turn on here.
                    try
                    {
                        if (cmaster.IsRadioCreated)
                        {
                            int ddc = GetDdcForRx(rx);
                            if (ddc >= 0) NetworkIO.VFOfreq(ddc, slice.FrequencyMHz, 0);
                        }
                        cmaster.SetRXTCIRun(1);
                        cmaster.UpdateRXTCIRunState();
                    }
                    catch { }
                }

                audio = slice.IsStreamingAudio;
            }

            SliceStreamingChanged?.Invoke(rx, audio);
            return true;
        }

        /// <summary>Release one consumer. Returns true when it was actually present.</summary>
        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public bool RemoveConsumer(int rx, SliceConsumer consumer)
        {
            bool audio;
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return false;
                if (!slice.RemoveConsumer(consumer)) return false;

                slice.IsStreamingAudio = slice.HasAudioConsumer;
                slice.IsStreamingIQ = slice.HasConsumer(SliceConsumer.TciIq);

                bool updateTciRun = false;
                if (!slice.HasAnyConsumer)
                {
                    // last consumer: the slice really goes dormant (0% CPU)
                    if (slice.IsActive) DeactivateCoreLocked(slice);
                    updateTciRun = (consumer & (SliceConsumer.TciAudio | SliceConsumer.TciIq)) != 0;
                }
                else if ((consumer & SliceConsumer.TciAudio) != 0 && !slice.IsStreamingIQ)
                {
                    updateTciRun = true;      // no IQ left on this slice either
                }
                else if ((consumer & SliceConsumer.TciIq) != 0 && !slice.IsStreamingAudio)
                {
                    updateTciRun = true;      // no audio left on this slice either
                }

                if (updateTciRun)
                {
                    try { cmaster.UpdateRXTCIRunState(); } catch { }
                }
                audio = slice.IsStreamingAudio;
            }

            SliceStreamingChanged?.Invoke(rx, audio);
            return true;
        }

        /// <summary>H1: true when the given consumer currently holds the slice.</summary>
        public bool HasConsumer(int rx, SliceConsumer consumer)
        {
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return false;
                return slice.HasConsumer(consumer);
            }
        }

        /// <summary>H1: true when any front-end still holds the slice.</summary>
        public bool HasAnyConsumer(int rx)
        {
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return false;
                return slice.HasAnyConsumer;
            }
        }

        // H1: bring a slice's WDSP channel up.  Extracted from ActivateAudio so
        // every consumer kind shares the identical init sequence; the TCI-only
        // path keeps its original order of operations.
        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        private void EnsureActiveCoreLocked(HeadlessSlice slice)
        {
            if (cmaster.IsRadioCreated)
            {
                try
                {
                    // Ensure ChannelMaster router has all 8 sources active
                    cmaster.CMLoadRouterAll(HardwareSpecific.Model);

                    int inRate = GetSliceRateHz(slice.RxIndex);
                    slice.SampleRateHz = inRate;      // fallback for later queries
                    TciLog.Log($"[Slice] rx{slice.RxIndex} active rate={inRate}");
                    int ddc = GetDdcForRx(slice.RxIndex);
                    if (ddc >= 0) NetworkIO.SetDDCRate(ddc, inRate);
                    cmaster.SetXcmInrate(slice.RxIndex, inRate);

                    // Configure DSP parameters for slice channel
                    WDSP.SetRXAMode(slice.ChannelId, slice.Mode);
                    WDSP.SetRXABandpassFreqs(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                    WDSP.RXANBPSetFreqs(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                    WDSP.SetRXASNBAOutputBandwidth(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                    WDSP.SetRXAAGCMode(slice.ChannelId, slice.AgcMode);   // re-init from the slice's preset, not MED
                    WDSP.SetRXAAGCTop(slice.ChannelId, 90.0);
                    WDSP.SetRXAPanelGain1(slice.ChannelId, slice.AudioGain);

                    // Turn ON WDSP processing for this slice channel
                    WDSP.SetChannelState(slice.ChannelId, 1, 0);
                }
                catch { }
            }
            slice.IsActive = true;
        }

        // H1: turn a slice's WDSP channel off - only when the last consumer went.
        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        private void DeactivateCoreLocked(HeadlessSlice slice)
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

        // ---- TCI-facing wrappers ---------------------------------------------------
        // The TCI servers keep their own per-client/per-stream guards; these map
        // one TCI listener event to one consumer kind.

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void ActivateAudio(int rx)
        {
            AddConsumer(rx, SliceConsumer.TciAudio);
        }

        // Branch G: activate a slice for IQ streaming only (no audio client).
        // CW Skimmer requests IQ without audio; the WDSP channel and DDC must be
        // running for IQ to flow, so this shares the same activation core.
        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void ActivateIQ(int rx)
        {
            AddConsumer(rx, SliceConsumer.TciIq);
        }

        // Branch G: called when the last IQ client disconnects/stops.
        public void DeactivateIQ(int rx)
        {
            RemoveConsumer(rx, SliceConsumer.TciIq);
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
                    if (!s.HasAnyConsumer) continue;
                    // force the same full init the first activation did
                    s.IsActive = false;
                    EnsureActiveCoreLocked(s);
                    if (s.HasConsumer(SliceConsumer.TciAudio) || s.HasConsumer(SliceConsumer.TciIq))
                    {
                        try
                        {
                            int ddc = GetDdcForRx(s.RxIndex);
                            if (ddc >= 0) NetworkIO.VFOfreq(ddc, s.FrequencyMHz, 0);
                            cmaster.SetRXTCIRun(1);
                            cmaster.UpdateRXTCIRunState();
                        }
                        catch { }
                    }
                }
            }
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void DeactivateAudio(int rx)
        {
            RemoveConsumer(rx, SliceConsumer.TciAudio);
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        public void DeactivateAll()
        {
            // H1 (Policy A): a TCI shutdown releases the TCI consumers only. A
            // slice the GUI is displaying keeps its display consumer and stays up.
            lock (_lock)
            {
                foreach (var kvp in _slices)
                {
                    HeadlessSlice s = kvp.Value;
                    s.RemoveConsumer(SliceConsumer.TciAudio);
                    s.RemoveConsumer(SliceConsumer.TciIq);
                    s.IsStreamingAudio = s.HasAudioConsumer;
                    s.IsStreamingIQ = s.HasConsumer(SliceConsumer.TciIq);
                    if (!s.HasAnyConsumer && s.IsActive) DeactivateCoreLocked(s);
                }
                try { cmaster.UpdateRXTCIRunState(); } catch { }
            }

            SliceStreamingChanged?.Invoke(-1, false);
        }
    }
}
