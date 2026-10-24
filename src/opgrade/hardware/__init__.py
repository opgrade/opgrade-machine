from .base import Camera, Hardware, Lighting, LightingMode, Side


def build_hardware(simulation: bool, sim_optics: bool = False) -> Hardware:
    if simulation:
        from .sim import SimLens, SimulatedLighting, build_simulated_hardware

        if sim_optics:
            # Realistic optics to rehearse calibration: 6 mm lens distortion, LED colour cast, vignetting.
            return build_simulated_hardware(
                lens=SimLens(), lighting=SimulatedLighting(tint_bgr=(1.12, 1.0, 0.88), vignetting=0.12)
            )
        return build_simulated_hardware()
    from .pi import build_pi_hardware

    return build_pi_hardware()


__all__ = ["Camera", "Hardware", "Lighting", "LightingMode", "Side", "build_hardware"]
