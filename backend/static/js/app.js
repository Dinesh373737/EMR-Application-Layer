// Simple confirmation before submitting extracted data
function confirmSave() {
    return confirm("Do you want to save this extracted data into the EMR?");
}

document.addEventListener("DOMContentLoaded", function () {
    const loadingOverlay = document.getElementById("loading-overlay");

    if (!loadingOverlay) {
        console.error("Loading overlay element not found!");
        return;
    }

    // 1. Handle Link Clicks (Event Delegation)
    // This captures clicks on any anchor tag that is inside top-bar nav or is a major action button
    document.body.addEventListener("click", function (event) {
        // Find the closest anchor tag
        const link = event.target.closest("a");

        // If no link, or link has no href, or is a # link, ignore
        if (!link || !link.href || link.href.includes('#') || link.getAttribute('href').startsWith('#')) {
            return;
        }

        // Specific check: Only trigger for Navbar links or specific action buttons
        // We check if the link is inside the header nav OR is a button we want to track
        const isNav = link.closest("header nav");
        const isAction = link.classList.contains("btn-primary") || link.classList.contains("btn");

        // You can adjust this filter. Currently: Navbar links OR major buttons.
        // If you strictly only want Navbar, remove '|| isAction'
        if (isNav || isAction) {
            // Modifier keys check
            if (event.ctrlKey || event.metaKey || event.shiftKey || link.target === "_blank") {
                return;
            }

            // Show loader
            loadingOverlay.classList.remove("hidden");
        }
    });

    // 2. Handle Login Form Submission
    document.body.addEventListener("submit", function (event) {
        const form = event.target;

        // Check if form wants to suppress global loader
        if (form.dataset.noGlobalLoader === "true") {
            return;
        }

        // Simple check: if it's a form submission, show loader
        // (The browser will prevent submission if validation fails, so this is safe)
        loadingOverlay.classList.remove("hidden");
    });

    // 3. Handle Back Button (Restore state)
    // efficient way to hide loader if user goes back
    window.addEventListener("pageshow", function (event) {
        loadingOverlay.classList.add("hidden");
    });
});
