from .base import Camera, Hardware, Lighting, LightingMode, Side


def build_hardware(simulation: bool) -> Hardware:
    if simulation:
        from .sim import build_simulated_hardware

        return build_simulated_hardware()
    from .pi import build_pi_hardware

    return build_pi_hardware()


__all__ = ["Camera", "Hardware", "Lighting", "LightingMode", "Side", "build_hardware"]
