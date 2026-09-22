-- Keep alpinert loaded across core_levels.startLevel (not in the extension itself).
setExtensionUnloadMode("alpinert", "manual")
extensions.load("alpinert")
