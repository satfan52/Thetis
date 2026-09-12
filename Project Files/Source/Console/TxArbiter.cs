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
        private bool _isDigitalMox = false;
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

            if (_isDigitalMox)
            {
                _isDigitalMox = false;
                try
                {
                    if (_console != null && _console.MOX)
                        _console.MOX = false;
                }
                catch { }
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

        public bool IsDigitalMox => _isDigitalMox;

        public bool IsVoiceTransmitting
        {
            get
            {
                if (_isDigitalMox)
                {
                    if (_console != null)
                    {
                        var mode = _console.CurrentPTTMode;
                        if (mode == PTTMode.MIC || mode == PTTMode.VOX || mode == PTTMode.CW || mode == PTTMode.MANUAL)
                            return true;
                    }
                    return false;
                }

                if (_console != null && _console.MOX) return true;
                return Audio.MOX;
            }
        }

        private void OnMoxChanged(int rx, bool oldMox, bool newMox)
        {
            if (!newMox)
            {
                // If MOX turned off while digital slice was active, check if it was manually unkeyed by operator
                if (_isDigitalMox)
                {
                    int abortRx = -1;
                    lock (_lock)
                    {
                        if (_activeDigitalRx != -1)
                        {
                            abortRx = _activeDigitalRx;
                            _activeDigitalRx = -1;
                            _activeDigitalFrequency = 0;
                            _activeDigitalMode = DSPMode.DIGU;
                        }
                    }
                    _isDigitalMox = false;
                    if (abortRx != -1)
                    {
                        try
                        {
                            _console?.CIVControllerInstance?.ReleaseDigitalTx();
                        }
                        catch { }
                        DigitalSlicePreempted?.Invoke(abortRx);
                        DigitalTxStateChanged?.Invoke(-1);
                    }
                }
                return;
            }

            // Ignore MOX assertion triggered by our own digital slice!
            if (_isDigitalMox) return;

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

            // 1. Steer IC-7100 to slice frequency and DATA mode, force Simplex, then key CI-V PTT
            try
            {
                if (_console != null && _console.CIVControllerInstance != null && _console.CIVControllerInstance.IsOpen)
                {
                    _console.CIVControllerInstance.SteerAndKeyForDigitalTx(freqMHz, mode);
                }
            }
            catch { }

            // 2. Set Thetis MOX to true (with _isDigitalMox = true so OnMoxChanged does not self-preempt)
            if (_console != null && !_console.MOX)
            {
                _isDigitalMox = true;
                try
                {
                    if (_console.InvokeRequired)
                        _console.BeginInvoke(new Action(() => { if (!_console.MOX) _console.MOX = true; }));
                    else
                        _console.MOX = true;
                }
                catch { }
            }

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
                // 1. Release CI-V digital steering and restore IC-7100 state
                try
                {
                    if (_console != null && _console.CIVControllerInstance != null && _console.CIVControllerInstance.IsOpen)
                    {
                        _console.CIVControllerInstance.ReleaseDigitalTx();
                    }
                }
                catch { }

                // 2. Unkey Thetis MOX if it was keyed for digital slice
                if (_isDigitalMox)
                {
                    _isDigitalMox = false;
                    try
                    {
                        if (_console != null && _console.MOX)
                        {
                            if (_console.InvokeRequired)
                                _console.BeginInvoke(new Action(() => { if (_console.MOX) _console.MOX = false; }));
                            else
                                _console.MOX = false;
                        }
                    }
                    catch { }
                }

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

            if (_console != null && _isDigitalMox)
            {
                try
                {
                    _console.UpdateDigitalTxDdsFrequency(freqMHz);
                }
                catch { }
            }
        }
    }
}
