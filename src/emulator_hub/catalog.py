"""What the emulator image can boot. The single source of truth for both images.

`tests/test_catalog.py` asserts that every package listed here is installed by
`emulator/Dockerfile`, so a profile can never name a system image that isn't
baked in.
"""

from dataclasses import dataclass

FORM_FACTORS = ("phone", "tablet", "tv")


@dataclass(frozen=True)
class SystemImage:
    package: str
    api_level: int
    form_factors: tuple[str, ...]
    # The emulator raises guest RAM to this ("Increasing RAM size to 2560MB")
    # whatever -memory says, so Pod memory must be sized from it.
    min_ram_mb: int = 0


SYSTEM_IMAGES: dict[str, SystemImage] = {
    "android-35-google-apis": SystemImage(
        package="system-images;android-35;google_apis;x86_64",
        api_level=35,
        form_factors=("phone", "tablet"),
        min_ram_mb=2560,
    ),
    "android-36-android-tv": SystemImage(
        package="system-images;android-36;android-tv;x86_64",
        api_level=36,
        form_factors=("tv",),
    ),
}

# Guest RAM the emulator enforces for a device definition, whatever -memory
# says (from the emulator's own log: "Increasing RAM size to 4096MB").
DEVICE_MIN_RAM_MB: dict[str, int] = {"pixel_tablet": 4096, "medium_tablet": 4096}

# Extra host memory a device's display costs the emulator (gfxstream and
# SwiftShader framebuffers scale with the panel), on top of guest RAM.
DISPLAY_OVERHEAD_MB: dict[str, int] = {
    "pixel_8": 1024,
    "medium_phone": 1024,
    "pixel_tablet": 2048,
    "medium_tablet": 2048,
    "tv_1080p": 1024,
    "tv_720p": 1024,
}

# AVD hardware definitions (`avdmanager list device -c`) allowed per form factor.
DEVICES: dict[str, tuple[str, ...]] = {
    "phone": ("pixel_8", "medium_phone"),
    "tablet": ("pixel_tablet", "medium_tablet"),
    "tv": ("tv_1080p", "tv_720p"),
}

# Seeded into an empty database on first start.
DEFAULT_PROFILES: list[dict] = [
    {
        "name": "phone",
        "form_factor": "phone",
        "system_image": "android-35-google-apis",
        "device": "pixel_8",
        "ram_mb": 2048,
        "cores": 2,
    },
    {
        "name": "tablet",
        "form_factor": "tablet",
        "system_image": "android-35-google-apis",
        "device": "pixel_tablet",
        "ram_mb": 3072,
        "cores": 2,
    },
    {
        "name": "tv",
        "form_factor": "tv",
        "system_image": "android-36-android-tv",
        "device": "tv_1080p",
        "ram_mb": 2048,
        "cores": 2,
    },
]
