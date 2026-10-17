document.addEventListener("DOMContentLoaded", function () {
    const form = document.getElementById("team-create-form");
    if (!form) {
        return;
    }
    const player1 = document.getElementById("id_player1");
    const player2 = document.getElementById("id_player2");
    if (!player1 || !player2) {
        return;
    }
    const teamName = document.getElementById("id_team_name");

    function normalize(value) {
        return (value || "")
            .normalize("NFD")
            .replace(/[\u0300-\u036f]/g, "")
            .replace(/,/g, " ")
            .toLowerCase()
            .split(/\s+/)
            .filter(Boolean)
            .sort()
            .join(" ");
    }

    // Jugadores guardados (datalist «Apellido, Nombre»). Se leen todos una sola
    // vez: el nombre de la dupla se completa aunque el filtro de categoría
    // oculte algunas sugerencias.
    const datalist = document.getElementById("known-players");
    const allPlayers = [];
    if (datalist) {
        datalist.querySelectorAll("option").forEach(function (option) {
            const value = (option.value || "").trim();
            if (!value) {
                return;
            }
            allPlayers.push({ value: value, level: (option.dataset.level || "").trim() });
        });
    }

    // La clave ignora acentos y orden de las palabras, así «Ferreyra Brenda»
    // también encuentra a «Ferreyra, Brenda».
    const surnamesByKey = new Map();
    allPlayers.forEach(function (player) {
        if (player.value.includes(",")) {
            surnamesByKey.set(normalize(player.value), player.value.split(",")[0].trim());
        }
    });

    // Sugerencias por categoría del torneo: por defecto solo de esa categoría;
    // «Ver todas las categorías» habilita el resto sin recargar la página.
    const levelFilter = document.getElementById("show-all-levels");
    const levelHint = document.getElementById("level-filter-hint");
    const category = (form.dataset.category || "").trim();
    const categoryLabel = (form.dataset.categoryLabel || "").trim();

    function renderDatalist() {
        if (!datalist) {
            return;
        }
        const showAll = !levelFilter || levelFilter.checked || !category;
        const visible = showAll
            ? allPlayers
            : allPlayers.filter(function (player) {
                  return player.level === category;
              });
        datalist.innerHTML = "";
        visible.forEach(function (player) {
            const option = document.createElement("option");
            option.value = player.value;
            if (player.level) {
                option.dataset.level = player.level;
            }
            datalist.appendChild(option);
        });
        if (levelHint) {
            const count = visible.length + " jugadores libres";
            levelHint.textContent = showAll
                ? "Sugerencias: todas las categorías (" + count + ")."
                : "Sugerencias: " + (categoryLabel || category) + " (" + count + ").";
        }
    }

    if (levelFilter) {
        levelFilter.addEventListener("change", renderDatalist);
        renderDatalist();
    }

    function surnameOf(input) {
        const raw = (input.value || "").trim();
        if (!raw) {
            return "";
        }
        if (raw.includes(",")) {
            return raw.split(",")[0].trim();
        }
        const known = surnamesByKey.get(normalize(raw));
        if (known) {
            return known;
        }
        // Sin coma y sin coincidencia exacta: mismo criterio que el servidor
        // («Nombre Apellido…» -> el apellido es todo menos el primer nombre).
        const parts = raw.split(/\s+/).filter(Boolean);
        return parts.length > 1 ? parts.slice(1).join(" ") : "";
    }

    // El nombre sugerido sólo pisa al usuario si no lo editó a mano.
    let autoName = "";
    let nameTouched = false;

    if (teamName) {
        teamName.addEventListener("input", function () {
            nameTouched = teamName.value !== autoName;
        });
    }

    function refreshName() {
        if (!teamName || nameTouched) {
            return;
        }
        const first = surnameOf(player1);
        const second = surnameOf(player2);
        autoName = first && second ? first + " - " + second : "";
        if (teamName.value !== autoName) {
            teamName.value = autoName;
        }
    }

    function refreshValidity() {
        const first = normalize(player1.value);
        const second = normalize(player2.value);
        const samePerson = first !== "" && first === second;
        player2.setCustomValidity(
            samePerson ? "Los dos jugadores de la pareja no pueden ser la misma persona." : ""
        );
    }

    function refresh() {
        refreshValidity();
        refreshName();
    }

    player1.addEventListener("input", refresh);
    player2.addEventListener("input", refresh);
    form.addEventListener("submit", refresh);
    refresh();
});
