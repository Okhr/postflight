import json

from app.services.capabilities import (
    Capabilities,
    OpenCLDevice,
    backends_to_probe,
    parse_clinfo,
)

_RAW = {"CPU": 2, "GPU": 4, "ACCELERATOR": 8}


def clinfo(*platforms: tuple[str, list[tuple[str, str]]]) -> str:
    """Rebuild the shape `clinfo --json` really emits.

    Checked against clinfo 3.0.25 rather than guessed: `platforms` and `devices`
    are two **parallel** lists, and the devices of a platform hang off an `online`
    key instead of being nested inside it.
    """
    return json.dumps(
        {
            "platforms": [{"CL_PLATFORM_NAME": name} for name, _ in platforms],
            "devices": [
                {
                    "online": [
                        {
                            "CL_DEVICE_NAME": device,
                            "CL_DEVICE_TYPE": {
                                "raw": _RAW[kind],
                                "type": [f"CL_DEVICE_TYPE_{kind}"],
                            },
                        }
                        for device, kind in devices
                    ]
                }
                for _, devices in platforms
            ],
            "icd_loader": {"CL_ICD_LOADER_NAME": "ocl-icd"},
        }
    )


POCL_CPU = "cpu-haswell-Intel(R) Core(TM) i7-7700K CPU @ 4.20GHz"


def test_three_icds_one_cpu_device():
    """The real capture from 2026-08-17: mesa, rusticl and pocl all installed, and
    the only device in the whole machine is the CPU. Counting ICD files said "GPU"
    here, which is the bug this parser exists to kill."""
    devices = parse_clinfo(
        clinfo(
            ("Portable Computing Language", [(POCL_CPU, "CPU")]),
            ("rusticl", []),
            ("Clover", []),
        )
    )
    assert [(d.platform, d.kind) for d in devices] == [
        ("Portable Computing Language", "CPU")
    ]
    caps = Capabilities(opencl_devices=devices)
    assert caps.opencl_gpu is None
    assert caps.stabilize_device == "CPU"


def test_gpu_is_found_behind_the_cpu_platform():
    """pocl is platform #0 on this image, so the GPU is never first. Picking the
    first device rather than the first *GPU* would report the CPU."""
    devices = parse_clinfo(
        clinfo(
            ("Portable Computing Language", [(POCL_CPU, "CPU")]),
            ("NVIDIA CUDA", [("NVIDIA GeForce RTX 3090", "GPU")]),
        )
    )
    caps = Capabilities(opencl_devices=devices)
    assert caps.opencl_gpu == OpenCLDevice(
        platform="NVIDIA CUDA", name="NVIDIA GeForce RTX 3090", kind="GPU"
    )
    assert caps.stabilize_device == "NVIDIA GeForce RTX 3090 (NVIDIA CUDA)"


def test_device_type_as_a_bare_string():
    """Older clinfo spells the type as a plain string instead of an object."""
    payload = json.dumps(
        {
            "platforms": [{"CL_PLATFORM_NAME": "rusticl"}],
            "devices": [
                {"online": [{"CL_DEVICE_NAME": "AMD Radeon 890M", "CL_DEVICE_TYPE": "CL_DEVICE_TYPE_GPU"}]}
            ],
        }
    )
    assert parse_clinfo(payload)[0].kind == "GPU"


def test_nameless_device_is_dropped():
    payload = json.dumps(
        {
            "platforms": [{"CL_PLATFORM_NAME": "rusticl"}],
            "devices": [{"online": [{"CL_DEVICE_TYPE": {"type": ["CL_DEVICE_TYPE_GPU"]}}]}],
        }
    )
    assert parse_clinfo(payload) == []


def test_unreadable_output_never_raises():
    """A missing clinfo, a crash mid-output, a future schema: none of it may stop
    the worker from starting. The hardware chart is a nicety."""
    for payload in ("", "not json", "[]", "null", '{"platforms": null}', "{}"):
        assert parse_clinfo(payload) == []


def test_auto_probes_nvdec_before_vaapi():
    """A machine with both a discrete NVIDIA card and an iGPU: the card wins."""
    assert backends_to_probe("auto") == (["cuda", "vaapi"], "")


def test_a_pinned_backend_is_the_only_one_probed():
    assert backends_to_probe("vaapi") == (["vaapi"], "")
    assert backends_to_probe("cuda") == (["cuda"], "")


def test_cpu_probes_nothing_and_says_why():
    backends, note = backends_to_probe("cpu")
    assert backends == []
    assert "pinned to the CPU" in note


def test_value_is_normalized():
    assert backends_to_probe("  VAAPI ") == (["vaapi"], "")
    assert backends_to_probe("") == (["cuda", "vaapi"], "")


def test_unknown_value_falls_back_to_cpu_naming_the_value():
    backends, note = backends_to_probe("vulkan")
    assert backends == []
    assert "vulkan" in note


def test_default_capabilities_claim_nothing():
    """What the API reports before any probe has run, and what a machine with no
    accelerator keeps reporting."""
    caps = Capabilities()
    assert caps.hwaccel == "cpu"
    assert caps.decode_device is None
    assert caps.stabilize_device == "CPU"
