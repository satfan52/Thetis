//=================================================================
// TxArbiter.cs
//=================================================================
// Central TX arbitration and interlock for Release F.
// Guarantees:
// 1. Voice transmission (Mic VOX, Mic PTT, GUI MOX) has absolute priority
//    and immediately preempts any active digital transmission.
// 2. Digital transmissions follow LIFO preemption: last transmitter wins.
// 3. Radio steering and CI-V PTT are coordinated with CIVController.
//=================================================================

using System;

namespace Thetis
{
    public sealed class TxArbiter
    {
        public static TxArbiter Instance { get; } = new TxArbiter();

        private Console _console;
        private int _activeDigitalRx = -1; // -1 = idle
        private double _activeDigitalFrequency = 0;
        private DSPMode _activeDigitalMode = DSPMode.DIGU;
        private DSPMode _savedVoiceMode = DSPMode.USB;
        private bool _dspTxEngaged = false;
        private readonly object _lock = new object();

        /// <summary>
        /// Fired when an active digital slice is preempted (by voice or another slice).
        /// Parameter is the rx index (2..7) that was preempted.
        /// </summary>
        public event Action<int> DigitalSlicePreempted;

        /// <summary>
        /// Fired when digital TX state changes. Parameter is the active digital rx index (2..7), or -1 if idle.
        /// </summary>
        public event Action<int> DigitalTxStateChanged;

        private TxArbiter() { }

        public void Initialize(Console console)
        {
            _console = console;
            if (_console != null)
            {
                _console.MoxChangeHandlers += OnMoxChanged;
            }
        }

        public void Shutdown()
        {
            if (_console != null)
            {
                _console.MoxChangeHandlers -= OnMoxChanged;
            }

            bool wasActive = false;
            lock (_lock)
            {
                if (_dspTxEngaged && _console != null)
                {
                    try
                    {
                        cmaster.SetTXTCIAudioRun(0, 0);
                        cmaster.ResetTCITxState();
                        cmaster.SignalTciTxStream();
                        WDSP.SetChannelState(WDSP.id(1, 0), 0, 1);
                        var dspTx = _console.radio?.GetDSPTX(0);
                        if (dspTx != null)
                        {
                            dspTx.CurrentDSPMode = _savedVoiceMode;
                        }
                    }
                    catch { }
                    _dspTxEngaged = false;
                }

                if (_activeDigitalRx != -1)
                {
                    try
                    {
                        _console?.CIVControllerInstance?.ReleaseDigitalTx();
                    }
                    catch { }
                    _activeDigitalRx = -1;
                    _activeDigitalFrequency = 0;
                    _activeDigitalMode = DSPMode.DIGU;
                    wasActive = true;
                }
            }

            if (wasActive)
            {
                DigitalTxStateChanged?.Invoke(-1);
            }
        }

        public int ActiveDigitalRx
        {
            get
            {
                lock (_lock) { return _activeDigitalRx; }
            }
        }

        public double ActiveDigitalFrequency
        {
            get
            {
                lock (_lock) { return _activeDigitalFrequency; }
            }
        }

        public DSPMode ActiveDigitalMode
        {
            get
            {
                lock (_lock) { return _activeDigitalMode; }
            }
        }

        public bool IsVoiceTransmitting
        {
            get
            {
                if (_console != null && _console.MOX) return true;
                return Audio.MOX;
            }
        }

        private void OnMoxChanged(int rx, bool oldMox, bool newMox)
        {
            if (!newMox) return;

            // Voice MOX / VOX / Mic PTT activated! Immediately preempt any digital slice!
            int preemptedRx = -1;
            lock (_lock)
            {
                if (_activeDigitalRx != -1)
                {
                    preemptedRx = _activeDigitalRx;
                    _activeDigitalRx = -1;
                    _activeDigitalFrequency = 0;
                    _activeDigitalMode = DSPMode.DIGU;
                }
            }

            if (preemptedRx != -1)
            {
                try
                {
                    if (_dspTxEngaged && _console != null)
                    {
                        cmaster.SetTXTCIAudioRun(0, 0);
                        cmaster.ResetTCITxState();
                        cmaster.SignalTciTxStream();
                        _dspTxEngaged = false;
                    }
                }
                catch { }

                try
                {
                    _console?.CIVControllerInstance?.ReleaseDigitalTx();
                }
                catch { }
                DigitalSlicePreempted?.Invoke(preemptedRx);
                DigitalTxStateChanged?.Invoke(-1);
            }
        }

        public bool RequestDigitalTx(int rx, double freqMHz, DSPMode mode)
        {
            // Voice has absolute priority: reject digital TX if Voice is active
            if (IsVoiceTransmitting)
            {
                return false;
            }

            int preemptedRx = -1;
            lock (_lock)
            {
                // If another digital slice was transmitting, preempt it (LIFO)
                if (_activeDigitalRx != -1 && _activeDigitalRx != rx)
                {
                    preemptedRx = _activeDigitalRx;
                    _activeDigitalRx = -1;
                }

                _activeDigitalRx = rx;
                _activeDigitalFrequency = freqMHz;
                _activeDigitalMode = mode;
            }

            if (preemptedRx != -1)
            {
                DigitalSlicePreempted?.Invoke(preemptedRx);
            }

            DigitalTxStateChanged?.Invoke(rx);

            // Engage WDSP TX channel and route TCI audio
            try
            {
                if (_console != null)
                {
                    var dspTx = _console.radio?.GetDSPTX(0);
                    if (dspTx != null)
                    {
                        if (!_dspTxEngaged)
                        {
                            _savedVoiceMode = dspTx.CurrentDSPMode;
                        }
                        dspTx.CurrentDSPMode = mode;
                    }

                    WDSP.SetChannelState(WDSP.id(1, 0), 1, 0);
                    cmaster.SetTXTCIAudioRun(0, 1);
                    cmaster.SignalTciTxStream();
                    _dspTxEngaged = true;
                }
            }
            catch { }

            // Steer IC-7100 to slice frequency and DATA mode, force Simplex, then key CI-V PTT
            try
            {
                if (_console != null && _console.CIVControllerInstance != null && _console.CIVControllerInstance.IsOpen)
                {
                    _console.CIVControllerInstance.SteerAndKeyForDigitalTx(freqMHz, mode);
                }
            }
            catch { }

            return true;
        }

        public void ReleaseDigitalTx(int rx)
        {
            bool wasActive = false;
            lock (_lock)
            {
                if (_activeDigitalRx == rx)
                {
                    _activeDigitalRx = -1;
                    _activeDigitalFrequency = 0;
                    _activeDigitalMode = DSPMode.DIGU;
                    wasActive = true;
                }
            }

            if (wasActive)
            {
                try
                {
                    if (_dspTxEngaged && _console != null)
                    {
                        cmaster.SetTXTCIAudioRun(0, 0);
                        cmaster.ResetTCITxState();
                        cmaster.SignalTciTxStream();
                        WDSP.SetChannelState(WDSP.id(1, 0), 0, 1);
                        var dspTx = _console.radio?.GetDSPTX(0);
                        if (dspTx != null)
                        {
                            dspTx.CurrentDSPMode = _savedVoiceMode;
                        }
                        _dspTxEngaged = false;
                    }
                }
                catch { }

                // Release CI-V digital steering and restore IC-7100 state
                try
                {
                    if (_console != null && _console.CIVControllerInstance != null && _console.CIVControllerInstance.IsOpen)
                    {
                        _console.CIVControllerInstance.ReleaseDigitalTx();
                    }
                }
                catch { }

                DigitalTxStateChanged?.Invoke(-1);
            }
        }

        public void UpdateDigitalTxFrequency(int rx, double freqMHz)
        {
            lock (_lock)
            {
                if (_activeDigitalRx != rx) return;
                _activeDigitalFrequency = freqMHz;
            }

            try
            {
                if (_console != null && _console.CIVControllerInstance != null && _console.CIVControllerInstance.IsOpen)
                {
                    _console.CIVControllerInstance.UpdateDigitalTxFrequency(freqMHz);
                }
            }
            catch { }
        }
    }
}
