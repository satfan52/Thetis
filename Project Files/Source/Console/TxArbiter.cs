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
                }
            }

            if (preemptedRx != -1)
            {
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
            }

            if (preemptedRx != -1)
            {
                DigitalSlicePreempted?.Invoke(preemptedRx);
            }

            DigitalTxStateChanged?.Invoke(rx);

            // Steer IC-7100 to slice frequency and DATA mode, then key CI-V PTT
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
                    wasActive = true;
                }
            }

            if (wasActive)
            {
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
    }
}
