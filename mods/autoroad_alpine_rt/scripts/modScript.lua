-- Keep alpine_rt loaded across core_levels.startLevel (not in the extension itself).
setExtensionUnloadMode("alpine_rt", "manual")
extensions.load("alpine_rt")
