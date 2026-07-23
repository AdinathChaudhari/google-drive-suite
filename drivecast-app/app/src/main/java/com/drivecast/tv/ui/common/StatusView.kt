package com.drivecast.tv.ui.common

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.tv.material3.Button
import androidx.tv.material3.Icon
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import com.drivecast.tv.ui.theme.TextPrimary
import com.drivecast.tv.ui.theme.TextSecondary

/**
 * A designed replacement for ad-hoc error/empty/offline strings. An error is never a dead
 * end: when [primaryLabel] is supplied, the D-pad always lands on a focused action.
 */
@Composable
fun StatusView(
    modifier: Modifier = Modifier,
    icon: ImageVector? = null,
    title: String,
    body: String,
    primaryLabel: String? = null,
    onPrimary: (() -> Unit)? = null,
    secondaryLabel: String? = null,
    onSecondary: (() -> Unit)? = null,
) {
    val primaryFocus = remember { FocusRequester() }
    LaunchedEffect(primaryLabel) {
        if (primaryLabel != null) {
            runCatching { primaryFocus.requestFocus() }
        }
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Column(
            modifier = Modifier.widthIn(max = 560.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            if (icon != null) {
                Icon(
                    imageVector = icon,
                    contentDescription = null,
                    tint = TextSecondary,
                    modifier = Modifier
                        .size(56.dp)
                        .padding(bottom = 16.dp),
                )
            }
            Text(
                text = title,
                style = MaterialTheme.typography.titleLarge,
                color = TextPrimary,
                textAlign = TextAlign.Center,
            )
            Text(
                text = body,
                style = MaterialTheme.typography.bodyLarge,
                color = TextSecondary,
                textAlign = TextAlign.Center,
                maxLines = 3,
                modifier = Modifier.padding(top = 8.dp),
            )
            if (primaryLabel != null || secondaryLabel != null) {
                Row(
                    modifier = Modifier.padding(top = 24.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    if (primaryLabel != null) {
                        Button(
                            onClick = { onPrimary?.invoke() },
                            modifier = Modifier.focusRequester(primaryFocus),
                        ) {
                            Text(primaryLabel)
                        }
                    }
                    if (secondaryLabel != null) {
                        Button(onClick = { onSecondary?.invoke() }) {
                            Text(secondaryLabel)
                        }
                    }
                }
            }
        }
    }
}

@Preview
@Composable
private fun StatusViewPreview() {
    StatusView(
        title = "Can't reach your server",
        body = "Check that it's running and on the same network.",
        primaryLabel = "Retry",
        secondaryLabel = "Server settings",
    )
}
