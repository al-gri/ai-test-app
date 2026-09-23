"use strict";

(function () {
  var API_URL = "/api/calculate";
  var MAX_HISTORY = 10;

  var display = document.getElementById("display");
  var errorBox = document.getElementById("error");
  var historyList = document.getElementById("history");

  var expression = "";
  var history = [];
  var busy = false;

  function renderDisplay() {
    display.textContent = expression;
  }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function clearError() {
    errorBox.textContent = "";
    errorBox.hidden = true;
  }

  function renderHistory() {
    historyList.textContent = "";
    if (history.length === 0) {
      var empty = document.createElement("li");
      empty.className = "history__empty";
      empty.textContent = "No calculations yet.";
      historyList.appendChild(empty);
      return;
    }
    for (var i = history.length - 1; i >= 0; i -= 1) {
      var entry = history[i];
      var item = document.createElement("li");
      item.className = "history__item";

      var exprSpan = document.createElement("span");
      exprSpan.className = "history__expr";
      exprSpan.textContent = entry.expression + " =";

      var resultSpan = document.createElement("span");
      resultSpan.className = "history__result";
      resultSpan.textContent = entry.result;

      item.appendChild(exprSpan);
      item.appendChild(resultSpan);
      historyList.appendChild(item);
    }
  }

  function addHistory(expr, result) {
    history.push({ expression: expr, result: result });
    if (history.length > MAX_HISTORY) {
      history = history.slice(history.length - MAX_HISTORY);
    }
    renderHistory();
  }

  function insert(token) {
    if (busy) {
      return;
    }
    expression += token;
    clearError();
    renderDisplay();
  }

  function clearAll() {
    expression = "";
    clearError();
    renderDisplay();
  }

  function deleteLast() {
    if (busy) {
      return;
    }
    expression = expression.slice(0, -1);
    clearError();
    renderDisplay();
  }

  function setExpression(value) {
    expression = value;
    clearError();
    renderDisplay();
  }

  function calculate() {
    if (busy) {
      return;
    }
    var trimmed = expression.trim();
    if (trimmed === "") {
      showError("Enter an expression first");
      return;
    }
    busy = true;
    clearError();
    fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expression: trimmed })
    })
      .then(function (response) {
        return response.json().catch(function () {
          return { error: "Unexpected server response" };
        }).then(function (data) {
          return { ok: response.ok, data: data };
        });
      })
      .then(function (outcome) {
        if (outcome.ok && outcome.data && typeof outcome.data.result === "string") {
          var result = outcome.data.result;
          addHistory(trimmed, result);
          setExpression(result);
        } else {
          var message =
            outcome.data && typeof outcome.data.error === "string"
              ? outcome.data.error
              : "Could not calculate";
          showError(message);
        }
      })
      .catch(function () {
        showError("Network error. Please try again.");
      })
      .then(function () {
        busy = false;
      });
  }

  document.querySelectorAll("[data-insert]").forEach(function (button) {
    button.addEventListener("click", function () {
      insert(button.getAttribute("data-insert"));
    });
  });

  document.querySelectorAll("[data-action]").forEach(function (button) {
    button.addEventListener("click", function () {
      var action = button.getAttribute("data-action");
      if (action === "clear") {
        clearAll();
      } else if (action === "delete") {
        deleteLast();
      } else if (action === "calculate") {
        calculate();
      }
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }
    var key = event.key;

    if (key >= "0" && key <= "9") {
      insert(key);
      event.preventDefault();
      return;
    }
    if (key === "." || key === ",") {
      insert(".");
      event.preventDefault();
      return;
    }
    if (key === "+" || key === "-" || key === "*" || key === "/") {
      insert(key);
      event.preventDefault();
      return;
    }
    if (key === "(" || key === ")") {
      insert(key);
      event.preventDefault();
      return;
    }
    if (key === "Enter" || key === "=") {
      calculate();
      event.preventDefault();
      return;
    }
    if (key === "Backspace") {
      deleteLast();
      event.preventDefault();
      return;
    }
    if (key === "Escape" || key === "Delete") {
      clearAll();
      event.preventDefault();
    }
  });

  renderDisplay();
  renderHistory();
})();
