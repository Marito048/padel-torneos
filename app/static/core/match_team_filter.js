document.addEventListener("DOMContentLoaded", function () {
    const zoneField = document.getElementById("id_zone");
    const team1Field = document.getElementById("id_team1");
    const team2Field = document.getElementById("id_team2");
    if (!zoneField || !team1Field || !team2Field) {
        return;
    }

    const cache = {};

    async function fetchTeams(zoneId) {
        if (cache[zoneId]) {
            return cache[zoneId];
        }
        const response = await fetch("/gestion/api/zonas/" + encodeURIComponent(zoneId) + "/equipos/");
        if (!response.ok) {
            cache[zoneId] = [];
            return [];
        }
        const data = await response.json();
        cache[zoneId] = data.results || [];
        return cache[zoneId];
    }

    function rebuild(field, teams) {
        const currentValue = field.value;
        const optionValues = new Set(["", currentValue]);
        teamMap = teams.map(function (item) { return String(item.id); });
        while (field.options.length > 0) {
            field.remove(0);
        }
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "---------";
        field.appendChild(placeholder);
        for (const team of teams) {
            const option = document.createElement("option");
            option.value = team.id;
            option.textContent = team.text;
            if (String(team.id) === currentValue) {
                option.selected = true;
            }
            field.appendChild(option);
        }
        // If the previous selection no longer belongs to the zone, clear it.
        if (!teamMap.includes(currentValue)) {
            field.value = "";
        }
    }

    async function sync() {
        const zoneId = zoneField.value;
        const teams = zoneId ? await fetchTeams(zoneId) : [];
        rebuild(team1Field, teams);
        rebuild(team2Field, teams);
    }

    zoneField.addEventListener("change", sync);
    sync();
});