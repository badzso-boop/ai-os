// AI-OS Simulation Seed Data
const simEntities = [];

if (typeof window !== "undefined") {
    window.SIM_SEED = {
        title: "FreshBox",
        entities: simEntities,
        initialized: true
    };
}

if (typeof module !== "undefined" && module.exports) {
    module.exports = { simEntities };
}
