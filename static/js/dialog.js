(function() {

  $(document).ready(function() {

    var baseUrl = $("meta[name=base-url]").attr("content");

    AP.register({
      'dialog-button-click': function (event, cb) {
        if (event.action === "dialog.submit") {
          AP.require("dialog", function(dialog) {
            dialog.updatePrimaryAction({
              enabled: false
            });
          });

          $.ajax({
            url: baseUrl + "/create",
            type: "POST",
            contentType: "application/json; charset=UTF-8",
            data: JSON.stringify({
              message: $(".create-dialog textarea").val()
            })
          }).done(function() {
            cb(true);
          }).fail(function() {

            AP.require("dialog", function(dialog) {
              dialog.updatePrimaryAction({
                enabled: true
              });
            });

          });
        } else {
          cb(true);
        }


      }
    });

  });



})();