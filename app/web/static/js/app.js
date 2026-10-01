// Progressive enhancement only. Every page works without JavaScript.
document.documentElement.classList.add("js");

// Sensitive actions: the password confirmation is a pop-up (a modal <dialog>). The same form is in
// the page as an open dialog, so without JavaScript it is simply shown there. Nothing is sent from
// here: the form posts to the server like any other, and the server decides.
(function () {
  "use strict";

  function openModal(dialog) {
    if (typeof dialog.showModal !== "function") {
      return; // an old browser: the dialog stays in the page
    }
    // Not close(): that fires a "close" event, which the handlers below take for a dismissal.
    dialog.removeAttribute("open");
    dialog.showModal();
    var field = dialog.querySelector('input[type="password"]');
    if (field) {
      field.focus();
    }
  }

  // 1. A confirmation page: the action was just launched, so ask at once.
  var launched = document.querySelector("dialog[data-reauth-popup]");
  if (launched && typeof launched.showModal === "function") {
    var dismissedOnce = false;
    launched.addEventListener("close", function () {
      // Escape closes the pop-up; the form goes back into the page instead of disappearing.
      if (!dismissedOnce) {
        dismissedOnce = true;
        launched.show();
      }
    });
    openModal(launched);
  }

  // 2. A page with sensitive forms (Security): ask when one of them is first used or submitted.
  var onDemand = document.querySelector("dialog[data-reauth-on-demand]");
  if (onDemand && typeof onDemand.showModal === "function") {
    var confirmed = onDemand.getAttribute("data-confirmed") === "yes";
    var dismissed = false;
    onDemand.removeAttribute("open"); // hidden until a sensitive action starts (no "close" event)
    // Escape fires "cancel" before the dialog closes and focus returns to the form ("close" comes
    // after): noting the dismissal here keeps the returning focus from reopening the pop-up.
    var dismiss = function () {
      dismissed = true;
    };
    onDemand.addEventListener("cancel", dismiss);
    onDemand.addEventListener("close", dismiss);
    var ask = function (event) {
      if (confirmed || onDemand.open) {
        return;
      }
      if (event.type === "focusin" && dismissed) {
        return;
      }
      if (event.type === "submit") {
        event.preventDefault(); // the server would refuse it: ask for the password first
      }
      openModal(onDemand);
    };
    Array.prototype.forEach.call(
      document.querySelectorAll("form[data-needs-reauth]"),
      function (form) {
        form.addEventListener("focusin", ask);
        form.addEventListener("submit", ask);
      }
    );
  }
})();
