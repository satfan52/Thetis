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
            // Headless slices 2..7 correspond to DDC 2..7 (RX3..RX8)
            for (int rx = 2; rx < 8; rx++)
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
                    NetworkIO.VFOfreq(rx, freqMHz, 0);
                }
                catch { }
            }

            SliceFrequencyChanged?.Invoke(rx, freqMHz);
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
                            NetworkIO.SetDDCRate(rx, inRate);
                            cmaster.SetXcmInrate(rx, inRate);

                            // Configure DSP parameters for slice channel
                            WDSP.SetRXAMode(slice.ChannelId, slice.Mode);
                            WDSP.SetRXABandpassFreqs(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                            WDSP.RXANBPSetFreqs(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                            WDSP.SetRXASNBAOutputBandwidth(slice.ChannelId, slice.FilterLow, slice.FilterHigh);
                            WDSP.SetRXAAGCMode(slice.ChannelId, AGCMode.MED);
                            WDSP.SetRXAAGCTop(slice.ChannelId, 90.0);
                            WDSP.SetRXAPanelGain1(slice.ChannelId, 1.0);

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
                        NetworkIO.VFOfreq(rx, slice.FrequencyMHz, 0);
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
        public void DeactivateAudio(int rx)
        {
            lock (_lock)
            {
                if (!_slices.TryGetValue(rx, out HeadlessSlice slice)) return;

                slice.IsStreamingAudio = false;

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
