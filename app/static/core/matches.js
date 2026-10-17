document.addEventListener("DOMContentLoaded", function () {
    const zoneSelect = document.getElementById("id_zone");
    const team1 = document.getElementById("id_team1");
    const team2 = document.getElementById("id_team2");
    const dataNode = document.getElementById("zone-map-data");
    if (!zoneSelect || !team1 || !team2 || !dataNode) {
        return;
    }
    let zones = [];
    try {
        zones = JSON.parse(dataNode.textContent);
    } catch (error) {
        zones = [];
    }
    const byId = {};
    for (const zone of zones) {
        byId[String(zone.id)] = zone.teams || [];
    }

    function rebuild(field, teams, keepValue) {
        field.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = teams.length ? "Seleccioná una pareja" : "Elegí primero la zona";
        field.appendChild(placeholder);
        for (const team of teams) {
            const option = document.createElement("option");
            option.value = team.id;
            option.textContent = team.label;
            if (String(team.id) === String(keepValue || "")) {
                option.selected = true;
            }
            field.appendChild(option);
        }
        if (keepValue && !teams.some((team) => String(team.id) === String(keepValue))) {
            field.value = "";
        }
    }

    function sync() {
        const teams = byId[zoneSelect.value] || [];
        rebuild(team1, teams, team1.value);
        rebuild(team2, teams, team2.value);
    }

    // Evita elegir la misma pareja en ambos lados.
    function excludeMirror(source, target) {
        const sourceValue = source.value;
        for (const option of target.options) {
            option.disabled = option.value !== "" && option.value === sourceValue;
        }
    }

    team1.addEventListener("change", function () {
        excludeMirror(team1, team2);
    });
    team2.addEventListener("change", function () {
        excludeMirror(team2, team1);
    });

    zoneSelect.addEventListener("change", sync);
    sync();
});
