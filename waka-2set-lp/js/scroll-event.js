$(document).ready(function () {

    $("#topBtn").hide();　//初期はボタンを非表示

    $(window).on("scroll", function () {

        // if ( $(this).scrollTop() > 100 && $(this).scrollTop() < document.getElementById( "main" ).scrollHeight - 1700 ) {
        //     $("#topBtn").fadeIn("fast"); //ページの上から100pxスクロールした時、ボタンをフェードインさせる
        // } else {
        //     $("#topBtn").fadeOut("fast"); //100px以内の時はボタンをフェードアウトさせる
        // }

        if ( $(this).scrollTop() < document.getElementById( "main" ).scrollHeight - 1500 ) {
            $("#topBtn").fadeIn("fast"); //ページの上から100pxスクロールした時、ボタンをフェードインさせる
        } else {
            $("#topBtn").fadeOut("fast"); //100px以内の時はボタンをフェードアウトさせる
        }

        //確認用
        // console.log("scrollPositon", scrollPosition);

        scrollHeight = $(document).height(); //ドキュメントの高さ取得
        scrollPosition = $(window).height() + $(window).scrollTop(); //現在地取得
        footHeight = $("footer").innerHeight(); //止めたい位置の高さ(今回はfooter)
        
        
        if (scrollHeight - scrollPosition <= footHeight) { 
            $("#topBtn").css({
                "position": "absolute", //ドキュメントの高さと現在地の差がfooterの高さ以下の時pisitionをabsoluteに変更
            });
        } else {
            $("#topBtn").css({
                "position": "fixed", //それ以外の場合は固定表示にする
            });
        }
    });
});
