(function () {
  // Damage-level explanatory copy
  const conditionCopy = {
    "Light Damage":
      "<strong>Light Damage</strong> means that the book shows one or more minor bumps to its edges or corners which are cosmetically unsatisfying but don't affect the book's usefulness.",
    "Moderate Damage":
      "<strong>Moderate Damage</strong> means that the book may have noticeable cosmetic damage to its exterior or a few pages which are wrinkled or torn.",
    "Heavy Damage":
      "<strong>Heavy Damage</strong> means that the book is somehow weakened, usually by damage to its spine that means it will have to be handled carefully to preserve its usefulness."
  };

  // NEW: discount map
  const discountByCondition = {
    "Light Damage": "15%",
    "Moderate Damage": "30%",
    "Heavy Damage": "60%"
  };

  function updateBlocks(title) {
    const conditionBlock = document.getElementById("damage-condition-dynamic");
    const discountBlock = document.getElementById("damage-discount-dynamic");

    if (conditionBlock) {
      const html = conditionCopy[title];
      if (html) {
        conditionBlock.innerHTML = html;
        conditionBlock.style.display = "block";
      } else {
        conditionBlock.style.display = "none";
      }
    }

    if (discountBlock) {
      const discount = discountByCondition[title];
      if (discount) {
        discountBlock.innerHTML =
          `These copies are offered at <strong>${discount} off</strong> our list price.`;
        discountBlock.style.display = "block";
      } else {
        discountBlock.style.display = "none";
      }
    }
  }

  function setupObserver() {
    const radios = document.querySelectorAll(
      "variant-radios input[type='radio']"
    );
    if (!radios.length) return;

    const selected = Array.from(radios).find(r => r.checked);
    if (selected) updateBlocks(selected.value);

    radios.forEach(radio => {
      radio.addEventListener("change", (e) => {
        updateBlocks(e.target.value);
      });
    });
  }

  window.addEventListener("load", setupObserver);
})();