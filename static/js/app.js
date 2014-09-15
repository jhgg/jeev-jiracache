app = angular.module('app', ['ngSanitize']);

app.controller('MainCtrl', function ($scope, $sce) {
    var ws = new WebSocket("ws://" + document.domain + ":8085/ws");
    $scope.results = [];

    $scope.seq = 0;

    var seqCbs = {};
    var seenIds = {};
    window.seenIds = seenIds;
    var cHandlers = {
        'update': function(i) {
            if (seenIds[i.key] !== undefined)
                angular.extend(seenIds[i.key], i);
            else
                seenIds[i.key] = i;

        },
        'updateraw': function(i) {
            if (($scope.issue && i.key == $scope.issue.key) || wantIssueKey == i.key) {
                $scope.setIssue(i);
            }
        },
        'updatesearch': function(i, q) {
            if (q != $scope.query)
                return;

            var lastFocusedKey;
            if ($scope.issue)
                lastFocusedKey = $scope.issue.key;

            $scope.results = i.map(function (v) {
                if (v.key) {
                    seenIds[v.key] = v;
                    return v;
                }
                return seenIds[v];
            });

            if (lastFocusedKey) {
                var issueIndex = findIssueIndex(lastFocusedKey);
                if (issueIndex == -1) {
                    $scope.issue = null;
                    issueIndex = 0;
                }

                $scope.setActiveIndex(issueIndex);
            }

        }
    };

    $(document).keydown(function (e) {
        if (e.keyCode == 40 || e.keyCode == 38) {
            $scope.$apply(function () {
                $scope.moveActive(e.keyCode == 40 ? 1 : -1)
            });
            e.preventDefault();
        }

        if (e.keyCode == 13) {
            var issue = $scope.results[$scope.activeIndex];
            if (issue) {
                window.open('https://silverlogic.atlassian.net/browse/' + issue.key, '_blank');
            }
            e.preventDefault();
        }
    });

    $scope.moveActive = function (delta) {
        $scope.setActiveIndex($scope.activeIndex + delta);
    };

    $scope.setActiveIndex = function(newIndex) {
        if (newIndex < 0 || newIndex >= $scope.results.length) return;
        $scope.activeIndex = newIndex;
        $scope.fetchAndDisplayIssue($scope.results[newIndex].key)
    };

    function findIssueIndex(key) {
        if (!$scope.results || !$scope.results.length)
            return -1;

        for(var i = 0, l = $scope.results.length; i < l; i++)
            if ($scope.results[i].key == key)
                return i;

        return -1;
    }

    ws.onmessage = function (msg) {
        var data = JSON.parse(msg.data);
        var cb;

        if (data.s !== undefined) {
            cb = seqCbs[data.s];
            cb && $scope.$apply(function () {
                cb(data.r);
            });
            delete seqCbs[data.s];

        } else if (data.c) {
            cb = cHandlers[data.c];
            cb && $scope.$apply(function() {
                cb(data.i, data.q);
            })
        }
    };

    $scope.$on('$destroy', function () {
        ws.close();
    });

    $scope.$watch('query', function (newVal, oldVal) {
        if (!newVal) {
            $scope.results = [];
            $scope.fetchAndDisplayIssue(null);
            return;
        }

        var seq = $scope.seq++;

        ws.send(JSON.stringify({
            "c": "query",
            "q": newVal,
            "s": seq
        }));

        seqCbs[seq] = function (result) {
            $scope.results = result.map(function (v) {
                if (v.key) {
                    seenIds[v.key] = v;
                    return v;
                }
                var ret = seenIds[v];
                return ret;
            });

            if ($scope.results.length) {
                $scope.activeIndex = 0;
                $scope.fetchAndDisplayIssue($scope.results[0].key);
            } else {
                $scope.fetchAndDisplayIssue(null);
            }
        }
    });

    var wantIssueKey = null;
    $scope.fetchAndDisplayIssue = function (key) {
        if (key == null) {
            $scope.issue = null;
            wantIssueKey = null;
            return;
        }

        if ($scope.issue && $scope.issue.key === key) return;
        var seq = $scope.seq++;

        ws.send(JSON.stringify({
            c: "get",
            key: key,
            s: seq,
            full: true
        }));

        wantIssueKey = key;

        seqCbs[seq] = function (issue) {
            if (issue.key == wantIssueKey) {
                $scope.setIssue(issue);
                wantIssueKey = null;
            }
        }
    };

    $scope.setIssue = function (issue) {
        $scope.issue = issue;
        $scope.issue.renderedFields.description = $sce.trustAsHtml($scope.issue.renderedFields.description);
    }


});

$(function () {
    var $els = $("#results, #issue");
    $(".search input").focus();

    adjustHeights();

    function adjustHeights() {
        var winHeight = $(window).height();
        $els.css('height', winHeight - 41)

    }
});