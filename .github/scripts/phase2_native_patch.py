from pathlib import Path

ROOT = Path("source")


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


# ---- ivac.h ---------------------------------------------------------------
p, text, bom, crlf = load("Project Files/Source/ChannelMaster/ivac.h")
text = once(
    text,
    "\tint stereo;\t\t\t\t\t\t// 1 for stereo; 0 otherwise\n",
    "\tint stereo;\t\t\t\t\t\t// 1 for stereo; 0 otherwise\n\tint output_only;\t\t\t\t// 1 for dedicated audio output with no PortAudio input\n",
    "ivac.h output_only field",
)
text = once(
    text,
    "extern __declspec(dllexport) void SetIVACaudioSize (int id, int size);\n",
    "extern __declspec(dllexport) void SetIVACaudioSize (int id, int size);\nextern __declspec(dllexport) void SetIVACOutputOnly (int id, int output_only);\n",
    "ivac.h output-only export",
)
save(p, text, bom, crlf)


# ---- ivac.c ---------------------------------------------------------------
p, text, bom, crlf = load("Project Files/Source/ChannelMaster/ivac.c")

# Native mixer policy. Output-only mode makes TXMON source 1 the sole active
# source, so AAMIX never waits for receiver-audio source 0.
helper_anchor = "__declspec (align (16))\t\t\tIVAC pvac[MAX_EXT_VACS];\n\n"
helper = r'''static void apply_ivac_mixer_mode(IVAC a)
{
	if (a->output_only)
	{
		// Source 0 = RX audio, source 1 = TX monitor.  Only TX monitor is active
		// in dedicated processed-TX mode, preventing any wait on RX timing.
		SetAAudioMixStates(a->mixer, 0, 3, 2);
		SetAAudioMixWhat(a->mixer, 0, 0, 0);
		SetAAudioMixWhat(a->mixer, 0, 1, 1);
	}
	else
	{
		// Restore legacy VAC behaviour exactly: both sources remain active and
		// MOX/MON determine which source(s) are actually mixed.
		SetAAudioMixStates(a->mixer, 0, 3, 3);
		if (!a->mox)
		{
			SetAAudioMixWhat(a->mixer, 0, 0, 1);
			SetAAudioMixWhat(a->mixer, 0, 1, a->mon ? 1 : 0);
		}
		else
		{
			SetAAudioMixWhat(a->mixer, 0, 0, 0);
			SetAAudioMixWhat(a->mixer, 0, 1, a->mon ? 1 : 0);
		}
	}
}

'''
text = once(text, helper_anchor, helper_anchor + helper, "insert mixer mode helper")

text = once(
    text,
    "\ta->iq_type = iq_type;\n\ta->stereo = stereo;\n\ta->iq_rate = iq_rate;\n",
    "\ta->iq_type = iq_type;\n\ta->stereo = stereo;\n\ta->output_only = 0;\n\ta->iq_rate = iq_rate;\n",
    "initialize output_only",
)

# Every AAMIX recreation must re-apply the output-only active mask.
mixer_create = "\t\ta->mixer = create_aamix(-1, id, a->audio_size, a->audio_size, 2, 3, 3, 1.0, 4096, inrate, a->audio_rate, xvac_out, 0.0, 0.0, 0.0, 0.0);\n"
count = text.count(mixer_create)
if count != 4:
    raise RuntimeError(f"mixer recreation count: expected 4, got {count}")
text = text.replace(mixer_create, mixer_create + "\t\tapply_ivac_mixer_mode(a);\n")

# VAC input must never leak/stale-feed TX while operating as output-only.
text = once(
    text,
    "\tIVAC a = pvac[id];\n\tif (a->run)\n",
    "\tIVAC a = pvac[id];\n\tif (a->output_only)\n\t{\n\t\tmemset(in_tx, 0, 2 * a->mic_size * sizeof(double));\n\t\treturn;\n\t}\n\tif (a->run)\n",
    "xvacIN output-only guard",
)

# Output-only callback path: no access to PortAudio input/rmatchIN.
callback_anchor = "\tif (!a->run) return 0;\n\t\t\t\t\t\t\t\t\t  // [2.10.3.12]MW0LGE handle mono input devices\n"
callback_new = "\tif (!a->run) return 0;\n\tif (a->output_only)\n\t{\n\t\txrmatchOUT(a->rmatchOUT, out_ptr);\n\t\treturn 0;\n\t}\n\t\t\t\t\t\t\t\t\t  // [2.10.3.12]MW0LGE handle mono input devices\n"
text = once(text, callback_anchor, callback_new, "CallbackIVAC output-only path")

# Replace PortAudio start function with a duplex-or-output-only implementation.
start = text.index("PORT int StartAudioIVAC(int id)\n{")
end = text.index("\nPORT void SetIVACRBReset", start)
new_start = r'''PORT int StartAudioIVAC(int id)
{
	IVAC a = pvac[id];
	int error = 0;
	int in_dev = paNoDevice;
	int out_dev = Pa_HostApiDeviceIndexToDeviceIndex(a->host_api_index, a->output_dev_index);

	int inChannelCount = 2;
	int outChannelCount = 2;

	const PaDeviceInfo* inDevInfo = NULL;
	if (!a->output_only)
	{
		in_dev = Pa_HostApiDeviceIndexToDeviceIndex(a->host_api_index, a->input_dev_index);
		if (in_dev >= 0)
		{
			inDevInfo = Pa_GetDeviceInfo(in_dev);
			if (inDevInfo != NULL)
			{
				inChannelCount = inDevInfo->maxInputChannels;
				if (inChannelCount > 2) inChannelCount = 2;
			}
		}
		if (inDevInfo == NULL || inChannelCount < 1) return -1;
	}

	const PaDeviceInfo* outDevInfo = NULL;
	if (out_dev >= 0)
		outDevInfo = Pa_GetDeviceInfo(out_dev);
	if (outDevInfo == NULL) return -1;

	if (!a->output_only)
	{
		a->inParam.device = in_dev;
		a->inParam.channelCount = inChannelCount;
		a->inParam.suggestedLatency = a->pa_in_latency;
		a->inParam.sampleFormat = paFloat64;
		a->inParam.hostApiSpecificStreamInfo = NULL;
	}

	a->outParam.device = out_dev;
	a->outParam.channelCount = outChannelCount;
	a->outParam.suggestedLatency = a->pa_out_latency;
	a->outParam.sampleFormat = paFloat64;
	a->outParam.hostApiSpecificStreamInfo = NULL;

	// Attempt exclusive mode for WASAPI devices.
	PaWasapiStreamInfo wasapiInputInfo;
	PaWasapiStreamInfo wasapiOutputInfo;
	if (!a->output_only && inDevInfo != NULL && a->exclusive_in)
	{
		const PaHostApiInfo* hostApiInfo = Pa_GetHostApiInfo(inDevInfo->hostApi);
		if (hostApiInfo != NULL && hostApiInfo->type == paWASAPI)
		{
			wasapiInputInfo.size = sizeof(PaWasapiStreamInfo);
			wasapiInputInfo.hostApiType = paWASAPI;
			wasapiInputInfo.version = 1;
			wasapiInputInfo.flags = (paWinWasapiExclusive | paWinWasapiThreadPriority);
			wasapiInputInfo.threadPriority = eThreadPriorityProAudio;
			a->inParam.hostApiSpecificStreamInfo = &wasapiInputInfo;
		}
	}
	if (outDevInfo != NULL && a->exclusive_out)
	{
		const PaHostApiInfo* hostApiInfo = Pa_GetHostApiInfo(outDevInfo->hostApi);
		if (hostApiInfo != NULL && hostApiInfo->type == paWASAPI)
		{
			wasapiOutputInfo.size = sizeof(PaWasapiStreamInfo);
			wasapiOutputInfo.hostApiType = paWASAPI;
			wasapiOutputInfo.version = 1;
			wasapiOutputInfo.flags = (paWinWasapiExclusive | paWinWasapiThreadPriority);
			wasapiOutputInfo.threadPriority = eThreadPriorityProAudio;
			a->outParam.hostApiSpecificStreamInfo = &wasapiOutputInfo;
		}
	}

	error = Pa_OpenStream(&a->Stream,
		a->output_only ? NULL : &a->inParam,
		&a->outParam,
		a->vac_rate,
		a->vac_size,
		0,
		CallbackIVAC,
		(void*)id);

	if (error != 0)
	{
		a->Stream = NULL;
		return -1;
	}

	error = Pa_StartStream(a->Stream);
	if (error != 0)
	{
		Pa_CloseStream(a->Stream);
		a->Stream = NULL;
		return -1;
	}

	return 1;
}
'''
text = text[:start] + new_start + text[end:]

text = once(
    text,
    "PORT void StopAudioIVAC(int id)\n{\n\tIVAC a = pvac[id];\n\tPa_CloseStream(a->Stream);\n}\n",
    "PORT void StopAudioIVAC(int id)\n{\n\tIVAC a = pvac[id];\n\tif (a->Stream != NULL)\n\t{\n\t\tPa_CloseStream(a->Stream);\n\t\ta->Stream = NULL;\n\t}\n}\n",
    "safe StopAudioIVAC",
)

# Replace duplicated legacy MOX/MON policy with one mode-aware helper.
policy_start = text.index("PORT void SetIVACmox(int id, int mox)\n{")
policy_end = text.index("\nPORT void SetIVACmonVol", policy_start)
new_policy = r'''PORT void SetIVACmox(int id, int mox)
{
	IVAC a = pvac[id];
	a->mox = mox;
	apply_ivac_mixer_mode(a);
}

PORT void SetIVACmon(int id, int mon)
{
	IVAC a = pvac[id];
	a->mon = mon;
	apply_ivac_mixer_mode(a);
}

PORT void SetIVACOutputOnly(int id, int output_only)
{
	IVAC a = pvac[id];
	a->output_only = output_only ? 1 : 0;
	apply_ivac_mixer_mode(a);
}
'''
text = text[:policy_start] + new_policy + text[policy_end:]

save(p, text, bom, crlf)

print("Applied Phase 2 native IVAC patch")
