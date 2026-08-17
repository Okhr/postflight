import json

from app.services.capabilities import (
    Capabilities,
    OpenCLDevice,
    backends_to_probe,
    parse_clinfo,
    parse_vulkaninfo,
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


# Real `vulkaninfo --summary` output, captured on the RTX 3090 box (2026-08-17).
VULKANINFO = """Devices:
========
GPU0:
\tapiVersion         = 1.3.242
\tdriverVersion      = 535.5.3.192
\tvendorID           = 0x10de
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA GeForce RTX 3090
\tdriverName         = NVIDIA
GPU1:
\tapiVersion         = 1.3.230
\tdriverVersion      = 0.0.1
\tvendorID           = 0x10005
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 15.0.6, 256 bits)
\tdriverName         = llvmpipe
"""


def test_software_rasterizer_is_not_a_gpu():
    """llvmpipe is listed as a device like any other and would happily be reported
    as a GPU. Its deviceType is what gives it away."""
    assert parse_vulkaninfo(VULKANINFO) == ["NVIDIA GeForce RTX 3090"]


def test_vulkan_without_a_driver_reports_nothing():
    """What vulkaninfo prints when the ICD cannot load its library, measured on a
    container where the runtime injected no graphics libs."""
    payload = (
        "ERROR: [Loader Message] Code 0 : libnvidia-glsi.so.535.261.03: cannot open "
        "shared object file\nCannot create Vulkan instance.\n"
    )
    assert parse_vulkaninfo(payload) == []
    assert parse_vulkaninfo("") == []


def test_integrated_gpu_counts_too():
    payload = (
        "Devices:\nGPU0:\n\tdeviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\n"
        "\tdeviceName = Intel(R) HD Graphics 630\n"
    )
    assert parse_vulkaninfo(payload) == ["Intel(R) HD Graphics 630"]


def test_opencl_gpu_wins_over_vulkan():
    """Gyroflow tries OpenCL first, so that is what we name when both exist."""
    caps = Capabilities(
        opencl_devices=[OpenCLDevice("NVIDIA CUDA", "NVIDIA GeForce RTX 3090", "GPU")],
        vulkan_devices=["NVIDIA GeForce RTX 3090"],
    )
    assert caps.stabilize_device == "NVIDIA GeForce RTX 3090 (NVIDIA CUDA)"
    assert caps.stabilize_on_gpu


def test_vulkan_alone_still_counts_as_a_gpu():
    """A host with a Vulkan driver and no OpenCL ICD: Gyroflow falls back to wgpu,
    so announcing "CPU" here would be the same lie as counting ICD files."""
    caps = Capabilities(vulkan_devices=["AMD Radeon 890M"])
    assert caps.stabilize_device == "AMD Radeon 890M (Vulkan)"
    assert caps.stabilize_on_gpu


def test_neither_path_means_cpu():
    caps = Capabilities(
        opencl_devices=[OpenCLDevice("Portable Computing Language", POCL_CPU, "CPU")]
    )
    assert caps.stabilize_device == "CPU"
    assert not caps.stabilize_on_gpu


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
