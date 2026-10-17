document.addEventListener("submit", function (event) {
    const form = event.target;
    if (form && form.dataset && form.dataset.confirm) {
        if (!window.confirm(form.dataset.confirm)) {
            event.preventDefault();
            event.stopPropagation();
        }
    }
}, true);
